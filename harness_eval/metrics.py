"""指標の計算。

- 試行スコア   = 合格したグレーダーの重み付き割合（not_run は分母から除外）
- 試行の合格   = スコア 1.0 かつ 失敗扱いの異常終了なし
- pass@1       = タスク内の有効試行のうち合格した割合（全体はタスク平均）
- pass^k       = タスク内で k 回連続で合格する確率。有効試行 n 回・合格 c 回のとき C(c,k)/C(n,k)。
                 n = k なら「k 試行すべて合格なら 1、そうでなければ 0」と同じ。n < k のタスクは算出しない。
- 比較用（*_cmp）= 配布物がないと通らない判定（skill_used / subagent_used / hook_used で 1 回以上を要求）を
                 除いた合否・スコア。基準プロファイルとの Δ はこちらで計算する（除かないと Δ が構造的に水増しされる）
- Δ            = 比較プロファイル − 基準プロファイル（両方で値があるタスクだけ、比較用の値で計算）
- 95% 区間     = タスク単位の差を復元抽出するブートストラップ（paired_bootstrap）。試行を独立な標本として数えない
- 合格あたりのコスト = 総コスト ÷ 合格試行数（コスト不明の試行があれば算出しない）
- 異常終了（rate_limit / auth など）の試行は「無効」として集計から除外する

リリース判定（gate）: 前回と今回の同じプロファイル・同じタスクの差の区間を、事前に決めた非劣性マージン m で判定する。
    fail          … 区間の上端 < −m（はっきり悪化）、安全違反の失敗分類がある、または比較できない
                    （k・LLM 判定の有無・タスク定義が違う、今回の結果にプロファイル・タスクがない、指標を算出できない）
    pass          … 区間の下端 ≥ −m（悪化していても m 以内と言える）
    inconclusive  … それ以外、または比較できたタスクが min_tasks 未満（少ないタスクでは区間が狭く出すぎる）
    max_regressions を指定したときは、悪化したタスクがその件数を超えれば fail（回帰予算。GRASP の R(c) ≤ R₀ に当たる）

タスク単位の改善・悪化（paired_outcomes）: 2 つのプロファイルで値があるタスクを、良くなった／悪くなった／同じ に分け、
    良くなった数と悪くなった数の正確な両側符号検定（= 不一致のタスクに対する正確 McNemar 検定）の p 値を付ける。
    試行ではなくタスクを単位にするので、同じタスクの試行の相関を数えすぎない。

昇格判定（promote）: 同じ実行の中で、候補（candidate）が現行（incumbent）より良いと言えるかを 3 値で返す。
    reject        … 比較できない・安全違反・候補と現行が同じ配布物・holdout 以外のタスクを含む（require_split 指定時）・
                    悪化したタスクが max_regressions を超える・改善の推定値が 0 以下
    promote       … 比較できたタスクが min_tasks 以上、区間の下端 > min_improvement、符号検定 p ≤ α / m
                    （m = この holdout で評価済みの候補の数。holdout を何度も見るほど偶然の勝ちが出やすいため）
    inconclusive  … それ以外（片側だけ値のあるタスク・キャッシュの試行を含むときも）
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from math import comb
from statistics import mean
from typing import Any, Iterable

from .record import TrialRecord

BOOTSTRAP_RESAMPLES = 10_000
HARD_CATEGORIES = ("unsafe_access",)  # 1 件でもあればリリースを止める失敗分類（harvest の「本物の環境への接続」）
METRICS = ("pass_at_1", "pass_hat_k", "mean_score")


@dataclass(frozen=True)
class PairedCI:
    n_tasks: int
    estimate: float | None
    low: float | None
    high: float | None


def paired_bootstrap(diffs: list[float], confidence: float = 0.95, resamples: int = BOOTSTRAP_RESAMPLES,
                     seed: int = 0) -> PairedCI:
    """タスクごとの差の平均と、タスクを復元抽出したパーセンタイル区間。同じ入力と seed なら同じ結果。"""
    n = len(diffs)
    if n == 0:
        return PairedCI(0, None, None, None)
    est = mean(diffs)
    if n == 1:
        return PairedCI(1, est, None, None)
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(resamples))
    alpha = (1 - confidence) / 2
    return PairedCI(n, est, means[int(alpha * resamples)], means[min(int((1 - alpha) * resamples), resamples - 1)])


def decide(ci: PairedCI, margin: float, min_tasks: int) -> str:
    """pass / fail / inconclusive（モジュール冒頭の規則）。"""
    if ci.low is None or ci.high is None or ci.n_tasks < min_tasks:
        return "inconclusive"
    if ci.high < -margin:
        return "fail"
    if ci.low >= -margin:
        return "pass"
    return "inconclusive"


def sign_test(improved: int, regressed: int) -> float:
    """正確な両側符号検定の p 値（同点は除く）。不一致のペアに対する正確 McNemar 検定と同じ。"""
    n = improved + regressed
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(min(improved, regressed) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired_outcomes(cur: dict[str, dict[str, Any]], base: dict[str, dict[str, Any]], m: str) -> dict[str, Any]:
    """タスクごとに cur と base の値 m を比べ、良くなった・悪くなったタスクと符号検定の p 値を返す（両方で値があるタスクだけ）。"""
    improved, regressed, tied = [], [], 0
    for t in sorted(cur):
        if t not in base or cur[t].get(m) is None or base[t].get(m) is None:
            continue
        d = cur[t][m] - base[t][m]
        if d > 1e-9:
            improved.append(t)
        elif d < -1e-9:
            regressed.append(t)
        else:
            tied += 1
    return {"improved": improved, "regressed": regressed, "tied": tied, "n": len(improved) + len(regressed) + tied,
            "sign_p": sign_test(len(improved), len(regressed))}


def split_half_noise(records: list[TrialRecord], profile_id: str, confidence: float = 0.95, resamples: int = 2000,
                     seed: int = 0, comparable: bool = False) -> dict[str, Any]:
    """同じプロファイルの試行をタスクごとに無作為に 2 分割し、「半分 − もう半分」の pass@1 の差（タスク平均）の範囲を出す。
    同じ配布物どうしでも、この程度の差は偶然で出る（A/A の雑音幅）。有効試行が 2 回未満のタスクは使わない。
    各側 h 試行の半分どうしの差は、各側 n 試行の比較より √(n/h) 倍ばらつくので、タスクごとに √(h/n) を掛けて
    「各側 n 試行の比較」に換算する（n が偶数なら 1/√2。奇数でも広く出しすぎない。合否の分散が同じとみなす近似）。
    comparable: 配布物がないと通らない判定を除いた合否（cmp_passed）で数える（promote が比較用の値を使うとき）。"""
    by_task: dict[str, list[bool]] = defaultdict(list)
    for r in records:
        if r.profile_id == profile_id and r.valid:
            ok = r.cmp_passed if comparable else r.passed
            if ok is not None:
                by_task[r.task_id].append(bool(ok))
    usable = [v for _, v in sorted(by_task.items()) if len(v) >= 2]
    if not usable:
        return {"n_tasks": 0, "low": None, "high": None}
    rng = random.Random(seed)
    diffs = []
    for _ in range(resamples):
        total = 0.0
        for v in usable:
            w = v[:]
            rng.shuffle(w)
            h = len(w) // 2
            total += (sum(w[:h]) / h - sum(w[h:2 * h]) / h) * (h / len(w)) ** 0.5
        diffs.append(total / len(usable))
    diffs.sort()
    alpha = (1 - confidence) / 2
    return {"n_tasks": len(usable), "low": diffs[int(alpha * resamples)],
            "high": diffs[min(int((1 - alpha) * resamples), resamples - 1)]}


def holdout_comparisons(rows: list[dict[str, Any]], tasks: dict[str, str], incumbent_hash: str | None) -> int:
    """この holdout のタスク（id → 中身のハッシュ）と 1 つでも重なる実行で評価された、現行以外の配布物の数。
    - プロファイル id を変えても同じ配布物（harness_hash）は 1 つと数える
    - 配布物なし（install が空）の基準は候補ではないので数えない
    - タスクを 1 つ足した・外した実行も「同じ holdout を見た」と数える（重なりで判定。完全一致にすると数え漏れる）
    - 形の違う行・値（文字列でない id やハッシュ）は読み飛ばす"""
    mine = set(tasks.items())
    seen: set[str] = set()
    for r in rows:
        rt, rp = (r.get("tasks"), r.get("profiles")) if isinstance(r, dict) else (None, None)
        if not isinstance(rt, dict) or not mine & {(t, h) for t, h in rt.items() if isinstance(h, str)}:
            continue
        for v in (rp.values() if isinstance(rp, dict) else ()):
            h = v.get("harness") if isinstance(v, dict) else None
            if isinstance(h, str) and h != incumbent_hash and not v.get("install_empty"):
                seen.add(h)
    return len(seen)


def pass_hat_k(n: int, c: int, k: int) -> float | None:
    if k <= 0 or n < k:
        return None
    return comb(c, k) / comb(n, k)


def _mean(xs: Iterable[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return mean(v) if v else None


def _trial_tokens(r: TrialRecord) -> int | None:
    """1 試行で使ったトークン。Claude はキャッシュ読み書きを input と別に数え、Codex はキャッシュが input の内数。"""
    keys = ("input", "output") if r.agent == "codex" else ("input", "cache_read", "cache_write", "output")
    vals = [(r.tokens or {}).get(k) for k in keys]
    return None if any(v is None for v in vals) else sum(vals)


def _mean_all(xs: list[float | None]) -> float | None:
    """どれか 1 つでも分からなければ None（分かる分だけの平均は、失敗した試行を外して安く見せてしまう）。"""
    return mean(xs) if xs and all(x is not None for x in xs) else None


def summarize(records: list[TrialRecord], k: int) -> dict[str, Any]:
    by_pt: dict[tuple[str, str], list[TrialRecord]] = defaultdict(list)
    for r in records:
        by_pt[(r.profile_id, r.task_id)].append(r)

    tasks: dict[str, dict[str, Any]] = {}
    for (pid, tid), rs in sorted(by_pt.items()):
        valid = [r for r in rs if r.valid]
        n, c = len(valid), sum(r.passed for r in valid)
        cmp = [r for r in valid if r.cmp_passed is not None]  # 比較用の判定が 1 つもない試行は Δ に使わない
        nc, cc = len(cmp), sum(bool(r.cmp_passed) for r in cmp)
        tasks.setdefault(pid, {})[tid] = {
            "trials": len(rs), "valid": n, "passed": c, "excluded": len(rs) - n,
            "pass_at_1": (c / n) if n else None, "pass_hat_k": pass_hat_k(n, c, k),
            "mean_score": _mean(r.score for r in valid),
            "pass_at_1_cmp": (cc / nc) if nc else None, "pass_hat_k_cmp": pass_hat_k(nc, cc, k),
            "mean_score_cmp": _mean(r.cmp_score for r in cmp),
            # 効率: エージェントの費用だけ（判定の費用は評価のための費用なので含めない）
            "mean_cost_usd": _mean_all([r.cost_usd for r in valid]),
            "mean_tokens": _mean_all([_trial_tokens(r) for r in valid]),
            # 開始時の文脈は成否によらない値なので、測れた試行だけで平均する（異常終了の 1 回で全体を不明にしない）
            "mean_context_tokens": _mean(r.meta.get("context_tokens") for r in valid),
        }

    profiles: dict[str, dict[str, Any]] = {}
    for pid in sorted({r.profile_id for r in records}):
        rs = [r for r in records if r.profile_id == pid]
        valid = [r for r in rs if r.valid]
        passed = [r for r in valid if r.passed]
        costs = [(r.cost_usd or 0) + (r.judge_cost_usd or 0) for r in valid]
        # エージェント・判定のどちらかの費用が分からない試行があれば総額は「不明」（0 で埋めない）
        cost_known = bool(valid) and all(r.cost_usd is not None and r.judge_cost_usd is not None for r in valid)
        total_cost = sum(costs)
        tags = Counter(t for r in valid if not r.passed for t in r.failure_tags)
        excl = Counter((r.error or {}).get("type", "?") for r in rs if not r.valid)
        comps = {"skills": set(), "plugins": set(), "mcp": set(), "agents": set()}
        for r in rs:
            comps["skills"].update(r.meta.get("loaded_skills") or [])
            comps["plugins"].update(r.meta.get("loaded_plugins") or [])
            comps["mcp"].update(r.meta.get("mcp_servers") or [])
            comps["agents"].update(map(str, r.meta.get("loaded_agents") or []))
        tv = tasks.get(pid, {})
        profiles[pid] = {
            "agent": rs[0].agent, "cli_version": rs[0].cli_version, "profile_hash": rs[0].profile_hash,
            "models": sorted({str(r.meta.get("model")) for r in rs if r.meta.get("model")}),
            "trials": len(rs), "valid": len(valid), "excluded": len(rs) - len(valid),
            "excluded_reasons": dict(excl), "passed": len(passed),
            "pass_at_1": _mean(v["pass_at_1"] for v in tv.values()),
            "pass_hat_k": _mean(v["pass_hat_k"] for v in tv.values()),
            "pass_hat_k_tasks": sum(1 for v in tv.values() if v["pass_hat_k"] is not None),
            "tasks": len(tv), "mean_score": _mean(r.score for r in valid),
            "total_cost_usd": total_cost if cost_known else None,
            "known_cost_usd": sum((r.cost_usd or 0) + (r.judge_cost_usd or 0) for r in valid),
            "cost_per_pass_usd": (total_cost / len(passed)) if (cost_known and passed) else None,
            "mean_duration_ms": _mean(r.duration_ms for r in valid),
            "mean_turns": _mean(r.num_turns for r in valid),
            "mean_context_tokens": _mean(r.meta.get("context_tokens") for r in valid),
            "tokens": {key: sum((r.tokens or {}).get(key) or 0 for r in valid)
                       for key in ("input", "output", "cache_read", "cache_write")},
            "failure_tags": dict(tags.most_common()),
            "cached_trials": sum(r.from_cache for r in rs),
            "components": {k2: sorted(v) for k2, v in comps.items()},
        }
    return {"k": k, "profiles": profiles, "tasks": tasks}


def _diff(a: dict[str, Any], b: dict[str, Any], m: str) -> float | None:
    return a[m] - b[m] if a.get(m) is not None and b.get(m) is not None else None


def deltas(summary: dict[str, Any], baseline: str) -> dict[str, dict[str, Any]]:
    """基準プロファイルとの差（同じエージェントに限らず計算する）。比較用の値（*_cmp）で計算し、95% 区間を付ける。"""
    out: dict[str, dict[str, Any]] = {}
    tasks = summary["tasks"]
    if baseline not in tasks:
        return out
    base = tasks[baseline]
    for pid, tv in tasks.items():
        if pid == baseline:
            continue
        common = [t for t in tv if t in base]
        # v0.3.0 までの summary.json には比較用の値がない → 全判定の値で計算し、comparable: False で知らせる
        cmp = all("pass_at_1_cmp" in tv[t] and "pass_at_1_cmp" in base[t] for t in common)
        sfx = "_cmp" if cmp else ""
        row: dict[str, Any] = {"common_tasks": len(common), "comparable": cmp}
        for m in METRICS:
            diffs = [d for t in common if (d := _diff(tv[t], base[t], m + sfx)) is not None]
            ci = paired_bootstrap(diffs)
            row[m] = ci.estimate
            row[m + "_ci"] = [ci.low, ci.high] if ci.low is not None else None
        pa, pb = summary["profiles"][pid], summary["profiles"][baseline]
        row["cost_per_pass_usd"] = (pa["cost_per_pass_usd"] - pb["cost_per_pass_usd"]
                                    if pa["cost_per_pass_usd"] is not None and pb["cost_per_pass_usd"] is not None else None)
        ctx = [d for t in common if (d := _diff(tv[t], base[t], "mean_context_tokens")) is not None]
        row["context_tokens"] = mean(ctx) if ctx else None  # 開始時の文脈の差 = 配布物が毎回足す固定費（共通タスクだけ）
        row["per_task"] = {t: {m: _diff(tv[t], base[t], m + sfx) for m in ("pass_at_1", "pass_hat_k")} for t in common}
        o = paired_outcomes(tv, base, "pass_at_1" + sfx)
        row["outcomes"] = {"improved": len(o["improved"]), "regressed": len(o["regressed"]), "tied": o["tied"], "sign_p": o["sign_p"]}
        out[pid] = row
    return out


def _comparability(current: dict[str, Any], previous: dict[str, Any], metric: str) -> list[str]:
    """実行全体として比べられるか（同じ k、LLM 判定の有無）。"""
    problems = []
    if metric == "pass_hat_k" and current.get("k") != previous.get("k"):
        problems.append(f"k が異なるため pass^k を比較できません（今回 {current.get('k')} / 前回 {previous.get('k')}）。"
                        "同じ --trials / --k で実行してください")
    cm, pm = current.get("meta", {}), previous.get("meta", {})
    tasks = [*cm.get("tasks", {}).values(), *pm.get("tasks", {}).values()]
    if any(t.get("llm", True) for t in tasks) and cm.get("options", {}).get("llm") != pm.get("options", {}).get("llm"):
        problems.append("LLM 判定の有無が前回と違うため比較できません（スモークとフルを比べていないか確認）")
    return problems


def gate(current: dict[str, Any], previous: dict[str, Any], metric: str, margin: float, min_tasks: int,
         extra_hard: Iterable[str] = (), max_regressions: int | None = None) -> dict[str, Any]:
    """前回（previous）と今回（current）の同じプロファイルを比べ、リリースしてよいかを 3 値で返す。

    戻り値: decision（pass / fail / inconclusive）、problems（比較できない・安全違反。1 つでもあれば fail）、
    regressions（前回より下がったタスクの一覧。診断用で、件数は max_regressions を指定したときだけ判定に使う）、
    profiles（プロファイル別の差・区間・改善と悪化の数・判定）。"""
    problems = _comparability(current, previous, metric)
    regressions: list[str] = []
    per_profile: dict[str, dict[str, Any]] = {}
    ct, pt = current.get("meta", {}).get("tasks", {}), previous.get("meta", {}).get("tasks", {})
    hard_categories = dict.fromkeys([*HARD_CATEGORIES, *extra_hard])  # 既定は常に含め、指定分を追加する
    for pid, cur in current.get("profiles", {}).items():
        hits = sorted(c for c in hard_categories if cur.get("failure_tags", {}).get(c))
        if hits:
            problems.append(f"{pid}: 安全違反の失敗分類 {hits} が今回の試行にあります")
    for pid, prev_tasks in previous["tasks"].items():
        cur_tasks = current["tasks"].get(pid)
        if cur_tasks is None:
            problems.append(f"{pid}: 今回の結果にプロファイルがありません")
            continue
        diffs = []
        improved = regressed = 0
        for tid, pv in prev_tasks.items():
            cv = cur_tasks.get(tid)
            if cv is None:
                problems.append(f"{pid}/{tid}: 今回の結果にタスクがありません")
                continue
            if tid in ct and tid in pt and ct[tid].get("hash") != pt[tid].get("hash"):
                problems.append(f"{pid}/{tid}: タスク定義・フィクスチャ・採点が前回と違うため比較できません")
                continue
            if pv.get(metric) is None:
                continue
            if cv.get(metric) is None:
                problems.append(f"{pid}/{tid}: {metric} を算出できません（有効試行 {cv['valid']} 回）")
                continue
            diffs.append(cv[metric] - pv[metric])
            if cv[metric] < pv[metric] - 1e-9:
                regressed += 1
                regressions.append(f"{pid}/{tid}: {metric} {pv[metric]:.2f} → {cv[metric]:.2f}")
            elif cv[metric] > pv[metric] + 1e-9:
                improved += 1
        ci = paired_bootstrap(diffs)
        dec = decide(ci, margin, min_tasks)
        if max_regressions is not None and regressed > max_regressions:
            problems.append(f"{pid}: 悪化したタスクが {regressed} 件で、回帰予算 {max_regressions} 件を超えています")
            dec = "fail"
        per_profile[pid] = {"n_tasks": ci.n_tasks, "estimate": ci.estimate,
                            "ci": [ci.low, ci.high] if ci.low is not None else None,
                            "improved": improved, "regressed": regressed, "sign_p": sign_test(improved, regressed),
                            "decision": dec}
    decisions = {v["decision"] for v in per_profile.values()}
    decision = ("fail" if problems or "fail" in decisions else
                "inconclusive" if "inconclusive" in decisions or not decisions else "pass")
    return {"decision": decision, "metric": metric, "margin": margin, "min_tasks": min_tasks, "max_regressions": max_regressions,
            "problems": problems, "regressions": regressions, "profiles": per_profile}


def _per_trial_tokens(p: dict[str, Any]) -> float | None:
    """有効試行 1 回あたりの入力側トークン（Claude は input + キャッシュ読み書き、Codex はキャッシュが input の内数なので input）。
    常時読み込まれる指示が増えるとここが増える。"""
    t = p.get("tokens") or {}
    if not p.get("valid"):
        return None
    keys = ("input",) if p.get("agent") == "codex" else ("input", "cache_read", "cache_write")
    total = sum((t.get(k) or 0) for k in keys)
    return total / p["valid"] if total else None


def _promote_problems(summary: dict[str, Any], candidate: str, incumbent: str, require_split: str | None,
                      extra_hard: Iterable[str]) -> tuple[list[str], bool]:
    """昇格判定に共通の「比べられない」理由と、配布物がないと通らない判定を外した値で比べるか（現行が配布物なしのとき）。"""
    problems: list[str] = []
    meta = summary.get("meta", {})
    tasks_all = summary.get("tasks", {})
    for pid in (candidate, incumbent):
        if pid not in tasks_all:
            problems.append(f"{pid}: この実行の結果にプロファイルがありません")
    mp = meta.get("profiles", {})
    hkey = lambda pid: (mp.get(pid) or {}).get("harness_hash") or (mp.get(pid) or {}).get("hash")  # v0.4.0 の結果は hash だけ
    if candidate in mp and incumbent in mp and hkey(candidate) == hkey(incumbent):
        problems.append("候補と現行が同じ配布物（配布物のハッシュが同じ）です")
    mt = meta.get("tasks", {})
    if require_split:
        wrong = sorted(t for t in set(tasks_all.get(candidate, {})) | set(tasks_all.get(incumbent, {}))
                       if (mt.get(t) or {}).get("split") != require_split)
        if wrong:
            problems.append(f"{require_split} 以外のタスク {wrong[:5]}{'…' if len(wrong) > 5 else ''} を含みます。"
                            f"昇格判定は改善に使っていない {require_split} のタスクだけで行ってください")
    hard = dict.fromkeys([*HARD_CATEGORIES, *extra_hard])
    cand_tags = summary.get("profiles", {}).get(candidate, {}).get("failure_tags", {})
    hits = sorted(c for c in hard if cand_tags.get(c))
    if hits:
        problems.append(f"{candidate}: 安全違反の失敗分類 {hits} があります")
    # 現行が配布物なしなら、配布物がないと通らない判定を両側とも外した値で比べる（deltas と同じ扱い）
    return problems, bool((mp.get(incumbent) or {}).get("install_empty"))


def _context_delta(ct: dict[str, dict[str, Any]], it: dict[str, dict[str, Any]]) -> float | None:
    """開始時の文脈の差（共通タスクの平均。配布物が毎回足す固定費の増減）。"""
    d = [x for t in ct if t in it and (x := _diff(ct[t], it[t], "mean_context_tokens")) is not None]
    return mean(d) if d else None


def promote(summary: dict[str, Any], records: list[TrialRecord], candidate: str, incumbent: str, metric: str, alpha: float,
            min_improvement: float, max_regressions: int, min_tasks: int, comparisons: int,
            require_split: str | None = "holdout", extra_hard: Iterable[str] = ()) -> dict[str, Any]:
    """候補を現行と同じ実行の中で比べ、昇格してよいかを 3 値で返す（モジュール冒頭の規則）。

    comparisons: この holdout で評価済みの候補の数 m（holdout_comparisons。最低 1）。有意水準は α / m に補正する。"""
    problems, cmp = _promote_problems(summary, candidate, incumbent, require_split, extra_hard)
    tasks_all = summary.get("tasks", {})
    m = metric + ("_cmp" if cmp else "")
    ct, it = tasks_all.get(candidate, {}), tasks_all.get(incumbent, {})
    diffs = [d for t in sorted(ct) if t in it and (d := _diff(ct[t], it[t], m)) is not None]
    # 片側だけ値のあるタスク（試行がすべて除外された・結果にない）は差に入らないが、そこに悪化が隠れうる。
    # 除外は環境起因でプロファイルごとに偏りうるので、こういうタスクがあれば昇格にしない
    one_sided = sorted(t for t in set(ct) | set(it) if ((ct.get(t) or {}).get(m) is None) != ((it.get(t) or {}).get(m) is None))
    ci = paired_bootstrap(diffs)
    o = paired_outcomes(ct, it, m)
    mm = max(1, int(comparisons))
    alpha_adj = alpha / mm
    # キャッシュの結果は別の時点の実行なので「同じ実行の中で比べる」前提が崩れる（holdout の実行は既定でキャッシュを使わない）
    cached = {pid: sum(1 for r in records if r.profile_id == pid and r.from_cache) for pid in (candidate, incumbent)}
    if len(o["regressed"]) > max_regressions:
        problems.append(f"悪化したタスクが {len(o['regressed'])} 件で、回帰予算 {max_regressions} 件を超えています: {o['regressed'][:5]}")
    if problems or (ci.estimate is not None and ci.estimate <= 0):
        decision = "reject"
    elif (ci.n_tasks < min_tasks or ci.low is None or ci.low <= min_improvement or o["sign_p"] > alpha_adj
          or len(o["improved"]) <= len(o["regressed"]) or any(cached.values()) or one_sided):
        # 符号検定は両側なので、悪化の方が多くても p が小さくなりうる → 改善したタスクが悪化より多いことも求める
        decision = "inconclusive"
    else:
        decision = "promote"
    pc, pi = summary.get("profiles", {}).get(candidate, {}), summary.get("profiles", {}).get(incumbent, {})
    tok_c, tok_i = _per_trial_tokens(pc), _per_trial_tokens(pi)
    return {"objective": "success", "decision": decision, "candidate": candidate, "incumbent": incumbent, "metric": metric,
            "comparable_metric": cmp, "n_tasks": ci.n_tasks, "estimate": ci.estimate, "ci": [ci.low, ci.high] if ci.low is not None else None,
            "improved": o["improved"], "regressed": o["regressed"], "tied": o["tied"], "sign_p": o["sign_p"],
            "alpha": alpha, "comparisons": mm, "alpha_adjusted": alpha_adj, "min_improvement": min_improvement,
            "max_regressions": max_regressions, "min_tasks": min_tasks, "problems": problems,
            "cached_trials": cached, "one_sided_tasks": one_sided,
            "noise": split_half_noise(records, incumbent, comparable=cmp),
            "cost_per_pass_delta_usd": (pc["cost_per_pass_usd"] - pi["cost_per_pass_usd"]
                                        if pc.get("cost_per_pass_usd") is not None and pi.get("cost_per_pass_usd") is not None else None),
            "input_tokens_per_trial": {"candidate": tok_c, "incumbent": tok_i}, "context_tokens_delta": _context_delta(ct, it)}


EFFICIENCY_METRICS = {"cost_usd": "mean_cost_usd", "tokens": "mean_tokens"}


def promote_efficiency(summary: dict[str, Any], records: list[TrialRecord], candidate: str, incumbent: str, metric: str,
                       alpha: float, margin: float, max_regressions: int, min_tasks: int, comparisons: int,
                       require_split: str | None = "holdout", extra_hard: Iterable[str] = (),
                       efficiency_metric: str = "cost_usd", anchor: str | None = None) -> dict[str, Any]:
    """効率の昇格判定: 成功率が現行より許容幅（margin）以上は悪化せず、タスクごとの費用がはっきり下がった候補だけを昇格する。
    - 見送り: 比べられない（共通の理由・回帰予算超過・エージェントや費用の出どころが違う）、または費用の推定が下がっていない
    - 昇格: 比べたタスクが min_tasks 以上、成功率の差の区間（信頼度 1 − 2α/m。下端が片側 α/m）の下端 ≥ −margin、
            タスクごとの費用比 log(候補/現行) の区間（同じ信頼度）の上端 < 0、費用の符号検定 p ≤ α/m かつ安くなったタスクが多い、
            キャッシュの試行・片側だけ値のあるタスク・費用の分からないタスクがない、anchor を指定したらそれにも成功率で劣らない
    - 判定不能: それ以外
    費用はエージェントの分だけ（判定の費用は含めない）。費用比を使うのは、高いタスク数件に結果を左右させないため。"""
    if efficiency_metric not in EFFICIENCY_METRICS:
        raise ValueError(f"efficiency_metric は {sorted(EFFICIENCY_METRICS)} のどれか")
    if not 0 < alpha < 0.5:
        raise ValueError("alpha は 0 より大きく 0.5 未満（片側の区間を信頼度 1 − 2α/m で作るため）")
    problems, cmp = _promote_problems(summary, candidate, incumbent, require_split, extra_hard)
    tasks_all = summary.get("tasks", {})
    by = lambda pid, attr: sorted({str(getattr(r, attr)) for r in records if r.profile_id == pid})
    if by(candidate, "agent") != by(incumbent, "agent"):
        problems.append(f"候補と現行でエージェントが違います（{by(candidate, 'agent')} / {by(incumbent, 'agent')}）。費用は同じエージェントどうしで比べます")
    elif efficiency_metric == "cost_usd" and by(candidate, "cost_source") != by(incumbent, "cost_source"):
        problems.append(f"候補と現行で費用の出どころ（cost_source）が違います（{by(candidate, 'cost_source')} / "
                        f"{by(incumbent, 'cost_source')}）。--efficiency-metric tokens で比べてください")
    m = metric + ("_cmp" if cmp else "")
    ct, it = tasks_all.get(candidate, {}), tasks_all.get(incumbent, {})
    mm = max(1, int(comparisons))
    alpha_adj = alpha / mm
    conf = 1 - 2 * alpha_adj
    o = paired_outcomes(ct, it, m)
    if len(o["regressed"]) > max_regressions:
        problems.append(f"悪化したタスクが {len(o['regressed'])} 件で、回帰予算 {max_regressions} 件を超えています: {o['regressed'][:5]}")
    s_ci = paired_bootstrap([d for t in sorted(ct) if t in it and (d := _diff(ct[t], it[t], m)) is not None], confidence=conf)
    cached = {pid: sum(1 for r in records if r.profile_id == pid and r.from_cache) for pid in (candidate, incumbent)}
    a_ci, a_one_sided = None, []
    if anchor is not None:
        at = tasks_all.get(anchor)
        if anchor in (candidate, incumbent):
            problems.append("--anchor には候補・現行とは別のプロファイルを指定します")
        elif at is None:
            problems.append(f"{anchor}: 基準（--anchor）のプロファイルがこの実行の結果にありません")
        else:
            # 基準が配布物なしなら、配布物がないと通らない判定を外した値で比べる。基準にないタスクは悪化を隠しうるので片側扱い
            am = metric + ("_cmp" if cmp or (summary.get("meta", {}).get("profiles", {}).get(anchor) or {}).get("install_empty") else "")
            a_one_sided = sorted(t for t in set(ct) | set(at) if ((ct.get(t) or {}).get(am) is None) != ((at.get(t) or {}).get(am) is None))
            a_ci = paired_bootstrap([d for t in sorted(ct) if t in at and (d := _diff(ct[t], at[t], am)) is not None], confidence=conf)
            cached[anchor] = sum(1 for r in records if r.profile_id == anchor and r.from_cache)
    key = EFFICIENCY_METRICS[efficiency_metric]
    both = sorted(set(ct) & set(it))
    unknown = [t for t in both if not (ct[t].get(key) or 0) > 0 or not (it[t].get(key) or 0) > 0]
    logs = {t: math.log(ct[t][key] / it[t][key]) for t in both if t not in unknown}
    c_ci = paired_bootstrap([logs[t] for t in sorted(logs)], confidence=conf)
    cheaper = [t for t in sorted(logs) if logs[t] < -1e-9]
    costlier = [t for t in sorted(logs) if logs[t] > 1e-9]
    cost_p = sign_test(len(cheaper), len(costlier))
    one_sided = sorted(t for t in set(ct) | set(it) if ((ct.get(t) or {}).get(m) is None) != ((it.get(t) or {}).get(m) is None))
    hint = ("費用の分からないタスクばかりです。--efficiency-metric tokens で比べてください" if both and len(unknown) == len(both)
            and efficiency_metric == "cost_usd" else "")
    if problems or (c_ci.estimate is not None and c_ci.estimate >= 0):
        decision = "reject"
    elif (min(s_ci.n_tasks, c_ci.n_tasks) < min_tasks or s_ci.low is None or s_ci.low < -margin
          or c_ci.high is None or c_ci.high >= 0 or cost_p > alpha_adj or len(cheaper) <= len(costlier)
          or any(cached.values()) or one_sided or unknown
          or (a_ci is not None and (a_ci.n_tasks < min_tasks or a_ci.low is None or a_ci.low < -margin or a_one_sided))):
        decision = "inconclusive"
    else:
        decision = "promote"
    return {"objective": "efficiency", "decision": decision, "candidate": candidate, "incumbent": incumbent, "metric": metric,
            "comparable_metric": cmp, "efficiency_metric": efficiency_metric, "margin": margin, "confidence": conf,
            "n_tasks": s_ci.n_tasks, "success_estimate": s_ci.estimate,
            "success_ci": [s_ci.low, s_ci.high] if s_ci.low is not None else None,
            "improved": o["improved"], "regressed": o["regressed"], "tied": o["tied"],
            "cost_tasks": c_ci.n_tasks, "log_cost_ratio": c_ci.estimate,
            "log_cost_ratio_ci": [c_ci.low, c_ci.high] if c_ci.low is not None else None,
            "relative_saving": (1 - math.exp(c_ci.estimate)) if c_ci.estimate is not None else None,
            "cheaper": cheaper, "costlier": costlier, "cost_sign_p": cost_p, "unknown_cost_tasks": unknown, "hint": hint,
            "anchor": anchor, "anchor_estimate": a_ci.estimate if a_ci else None,
            "anchor_ci": [a_ci.low, a_ci.high] if a_ci and a_ci.low is not None else None,
            "anchor_n_tasks": a_ci.n_tasks if a_ci else None, "anchor_one_sided_tasks": a_one_sided,
            "alpha": alpha, "comparisons": mm, "alpha_adjusted": alpha_adj, "max_regressions": max_regressions,
            "min_tasks": min_tasks, "problems": problems, "cached_trials": cached, "one_sided_tasks": one_sided,
            "context_tokens_delta": _context_delta(ct, it)}
