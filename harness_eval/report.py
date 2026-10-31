"""単一ファイルの HTML レポート（外部通信なし）。"""
from __future__ import annotations

import html
import json
from typing import Any

from .metrics import deltas
from .record import TrialRecord

CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e3e3e8;--card:#f7f7f9;--ok:#1a7f37;--ng:#c62828;--warn:#a15c00}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--mut:#9a9aa3;--line:#2c2c31;--card:#1c1c20;--ok:#56c271;--ng:#ff6b6b;--warn:#e0a03a}}
body{background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"Segoe UI","Hiragino Sans","Meiryo",sans-serif;margin:0;padding:24px;max-width:1280px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.mut{color:var(--mut)}table{border-collapse:collapse;width:100%;margin:6px 0;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}th{background:var(--card);font-weight:600;white-space:nowrap}
td.n{text-align:right}.ok{color:var(--ok);font-weight:600}.ng{color:var(--ng);font-weight:600}.warn{color:var(--warn)}
details{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin:4px 0}
summary{cursor:pointer}code,pre{font-family:ui-monospace,Consolas,monospace;font-size:12px}pre{white-space:pre-wrap;word-break:break-all;margin:4px 0}
.wrap{overflow-x:auto}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _usd(x: float | None) -> str:
    return "—" if x is None else f"${x:.4f}"


def _delta(x: float | None, pct: bool = True, lower_better: bool = False) -> str:
    if x is None:
        return "—"
    good = x < 0 if lower_better else x > 0
    cls = "ok" if good and abs(x) > 1e-9 else ("ng" if abs(x) > 1e-9 else "mut")
    txt = f"{x * 100:+.0f}pt" if pct else f"{x:+.4f}"
    return f'<span class="{cls}">{txt}</span>'


def _ci(ci: list[float] | None) -> str:
    return "" if not ci else f"<br><span class='mut'>[{ci[0] * 100:+.0f}, {ci[1] * 100:+.0f}]</span>"


def render(summary: dict[str, Any], records: list[TrialRecord], baseline: str | None = None) -> str:
    meta = summary.get("meta", {})
    k = summary["k"]
    P = summary["profiles"]
    out = [f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Harness Eval {_e(meta.get('run_id'))}</title><style>{CSS}</style></head><body>"]
    out.append(f"<h1>ハーネス評価レポート</h1><div class='mut'>run {_e(meta.get('run_id'))} ・ {_e(meta.get('started_at'))} ・ "
               f"試行 {_e(meta.get('options', {}).get('trials'))} 回/組 ・ k={k} ・ LLM判定 {'あり' if meta.get('options', {}).get('llm') else 'なし'}"
               f"（{_e(meta.get('options', {}).get('judge_agent'))}/{_e(meta.get('options', {}).get('judge_model'))}）</div>")
    if meta.get("ancestor_instructions"):
        out.append("<div class='warn'>注意: --out より上のフォルダの指示・スキルが全試行（配布物なしの基準を含む）に読み込まれた可能性があります: "
                   f"{_e(', '.join(meta['ancestor_instructions']))}</div>")

    out.append("<h2>サマリー</h2><div class='wrap'><table><tr><th>プロファイル</th><th>エージェント</th><th class='n'>pass^k</th>"
               "<th class='n'>pass@1</th><th class='n'>平均スコア</th><th class='n'>有効/全試行</th><th class='n'>総コスト</th>"
               "<th class='n'>合格あたり</th><th class='n'>平均時間</th><th class='n'>平均ターン</th><th class='n'>キャッシュ</th></tr>")
    for pid, s in P.items():
        dur = "—" if s["mean_duration_ms"] is None else f"{s['mean_duration_ms'] / 1000:.1f}s"
        turns = "—" if s["mean_turns"] is None else f"{s['mean_turns']:.1f}"
        cost = _usd(s["total_cost_usd"]) if s["total_cost_usd"] is not None else f"<span class='warn'>不明（既知 {_usd(s['known_cost_usd'])}）</span>"
        excl = f" <span class='warn'>除外 {s['excluded']}: {_e(s['excluded_reasons'])}</span>" if s["excluded"] else ""
        out.append(f"<tr><td><b>{_e(pid)}</b>{' <span class=mut>(基準)</span>' if pid == baseline else ''}</td><td>{_e(s['agent'])}</td>"
                   f"<td class='n'>{_pct(s['pass_hat_k'])} <span class='mut'>({s['pass_hat_k_tasks']}/{s['tasks']})</span></td>"
                   f"<td class='n'>{_pct(s['pass_at_1'])}</td><td class='n'>{_pct(s['mean_score'])}</td>"
                   f"<td class='n'>{s['valid']}/{s['trials']}{excl}</td><td class='n'>{cost}</td><td class='n'>{_usd(s['cost_per_pass_usd'])}</td>"
                   f"<td class='n'>{dur}</td><td class='n'>{turns}</td><td class='n'>{s['cached_trials']}</td></tr>")
    nr = meta.get("not_run") or 0
    if nr:
        out.append(f"<div class='warn'>未実行 {nr} 試行（--max-cost-usd / --monthly-budget-usd に到達）。集計に含めていません。</div>")
    out.append("</table></div><div class='mut'>pass^k の括弧内は「算出できたタスク数/全タスク数」（有効試行が k 回未満のタスクは算出しない）。"
               "除外 = レート制限・認証失敗など環境起因の異常終了で、集計に含めていません。</div>")

    if baseline and baseline in P:
        d = deltas(summary, baseline)
        if d:
            out.append(f"<h2>基準 <code>{_e(baseline)}</code> との差（Δ）</h2><div class='wrap'><table><tr><th>プロファイル</th><th class='n'>共通タスク</th>"
                       "<th class='n'>Δ pass^k</th><th class='n'>Δ pass@1</th><th class='n'>Δ 平均スコア</th><th class='n'>Δ 合格あたりコスト</th>"
                       "<th class='n'>改善／悪化（符号検定 p）</th></tr>")
            for pid, row in d.items():
                cells = "".join(f"<td class='n'>{_delta(row[m])}{_ci(row[m + '_ci'])}</td>" for m in ("pass_hat_k", "pass_at_1", "mean_score"))
                oc = row.get("outcomes")
                oc_cell = (f"<span class='ok'>{oc['improved']}</span> ／ <span class='ng'>{oc['regressed']}</span>"
                           f"<br><span class='mut'>p={oc['sign_p']:.3f}</span>") if oc else "—"
                out.append(f"<tr><td>{_e(pid)}</td><td class='n'>{row['common_tasks']}</td>{cells}"
                           f"<td class='n'>{_delta(row['cost_per_pass_usd'], pct=False, lower_better=True)}</td><td class='n'>{oc_cell}</td></tr>")
            out.append("</table></div><div class='mut'>Δ は両方で値があるタスクだけで計算。括弧内はタスク単位のブートストラップによる 95% 区間"
                       "（区間が 0 をまたぐなら差があるとは言えない）。配布物がないと通らない判定（スキル・サブエージェント・hook を"
                       "使うことの要求）は両側とも除いて比べる。合格あたりコストは負の値ほど安い。"
                       "タスクが少ない（目安 10〜20 件）と区間は実際より狭く出やすい。"
                       "改善／悪化は pass@1 が上がった・下がったタスクの数で、p は正確な両側符号検定"
                       "（平均が上がっていても悪化したタスクがあることを見落とさないため）。</div>")
            if not all(row["comparable"] for row in d.values()):
                out.append("<div class='warn'>この結果は v0.3.0 以前の形式のため、配布物がないと通らない判定を除けず、"
                           "全判定の値で Δ を計算しています（基準との差が大きめに出ることがあります）。</div>")

    tids = sorted({t for v in summary["tasks"].values() for t in v})
    out.append("<h2>タスク別（pass^k ／ pass@1 ／ 合格数/有効試行）</h2><div class='wrap'><table><tr><th>タスク</th>"
               + "".join(f"<th>{_e(p)}</th>" for p in P) + "</tr>")
    for t in tids:
        cells = []
        for p in P:
            v = summary["tasks"].get(p, {}).get(t)
            if not v:
                cells.append("<td class='mut'>対象外</td>")
                continue
            ph = v["pass_hat_k"]
            cls = "mut" if ph is None else ("ok" if ph >= 0.999 else "ng")
            cells.append(f"<td><span class='{cls}'>{_pct(ph)}</span> ／ {_pct(v['pass_at_1'])} ／ {v['passed']}/{v['valid']}</td>")
        out.append(f"<tr><td>{_e(t)}</td>{''.join(cells)}</tr>")
    out.append("</table></div>")

    out.append("<h2>失敗分類（不合格の有効試行に付いたタグ）</h2><table><tr><th>プロファイル</th><th>タグ（件数）</th></tr>")
    for pid, s in P.items():
        tags = ", ".join(f"{_e(k2)} ({v})" for k2, v in s["failure_tags"].items()) or "<span class='mut'>なし</span>"
        out.append(f"<tr><td>{_e(pid)}</td><td>{tags}</td></tr>")
    out.append("</table>")

    out.append("<h2>実行環境の記録</h2><table><tr><th>プロファイル</th><th>CLI</th><th>モデル</th><th>プロファイルハッシュ</th><th>読み込まれた構成要素</th></tr>")
    for pid, s in P.items():
        c = s["components"]
        comp = (f"<details><summary>スキル {len(c['skills'])} ／ プラグイン {len(c['plugins'])} ／ MCP {len(c['mcp'])} ／ エージェント {len(c.get('agents', []))}</summary>"
                f"<pre>skills: {_e(', '.join(c['skills']))}\nplugins: {_e(', '.join(c['plugins']))}\nmcp: {_e(', '.join(c['mcp']))}"
                f"\nagents: {_e(', '.join(c.get('agents', [])))}</pre></details>")
        out.append(f"<tr><td>{_e(pid)}</td><td>{_e(s['cli_version'])}</td><td>{_e(', '.join(s['models']))}</td>"
                   f"<td><code>{_e(s['profile_hash'])}</code></td><td>{comp}</td></tr>")
    out.append("</table><div class='mut'>構成要素は Claude Code の init イベントから取得（Codex は取得できないため空）。"
               "配布物以外が混入していないかをここで確認してください。</div>")

    out.append("<h2>試行の詳細</h2>")
    for r in records:
        mark = "<span class='ok'>PASS</span>" if r.passed else ("<span class='warn'>除外</span>" if not r.valid else "<span class='ng'>FAIL</span>")
        g_rows = "".join(
            f"<tr><td>{'✓' if g['status'] == 'pass' else ('·' if g['status'] == 'not_run' else '✗')}</td><td>{_e(g['label'])}</td>"
            f"<td>{_e(g['status'])}</td><td>{_e(g['reason'])}</td><td>{_e(g['category'])}</td></tr>" for g in r.grader_results)
        tools = ", ".join(c["name"] + ("!" if c.get("is_error") else "") for c in r.tool_calls)
        hooks = ", ".join(f"{h.get('name')}({h.get('outcome')})" for h in r.hook_calls or [])
        out.append(
            f"<details><summary>{mark} {_e(r.task_id)} / {_e(r.profile_id)} #{r.trial}"
            f" ・ score {_pct(r.score)} ・ {_usd(r.cost_usd)} ・ {r.num_turns or '—'} ターン"
            f"{' ・ キャッシュ' if r.from_cache else ''}{' ・ ' + _e(r.error['type']) if r.error else ''}</summary>"
            f"<table><tr><th></th><th>グレーダー</th><th>状態</th><th>理由</th><th>分類</th></tr>{g_rows}</table>"
            f"<pre>tools: {_e(tools) or '—'}\nskills: {_e(', '.join(r.skill_calls)) or '—'}{' (推定)' if r.skill_detection == 'heuristic' else ''}"
            f"\nsubagents: {_e(', '.join(r.subagent_calls)) or '—'}"
            f"\nhooks: {_e(hooks) or '—'}\nfiles created: {_e(', '.join(r.files_created)) or '—'}"
            f"\nerror: {_e(json.dumps(r.error, ensure_ascii=False)) if r.error else '—'}\nwarnings: {_e('; '.join(r.warnings)) or '—'}"
            f"\ntrial_dir: {_e(r.trial_dir)}</pre>"
            f"<details><summary>最終応答</summary><pre>{_e((r.final_message or '')[:4000])}</pre></details></details>")
    out.append("</body></html>")
    return "".join(out)
