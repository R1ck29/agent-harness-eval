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
    pass          … 区間の下端 ≥ −m（悪化していても m 以内と言える）
    inconclusive  … それ以外、または比較できたタスクが min_tasks 未満（少ないタスクでは区間が狭く出すぎる）
"""
from __future__ import annotations

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


def pass_hat_k(n: int, c: int, k: int) -> float | None:
    if k <= 0 or n < k:
        return None
    return comb(c, k) / comb(n, k)


def _mean(xs: Iterable[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return mean(v) if v else None


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
        row["per_task"] = {t: {m: _diff(tv[t], base[t], m + sfx) for m in ("pass_at_1", "pass_hat_k")} for t in common}
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
         hard_categories: Iterable[str] = HARD_CATEGORIES) -> dict[str, Any]:
    """前回（previous）と今回（current）の同じプロファイルを比べ、リリースしてよいかを 3 値で返す。

    戻り値: decision（pass / fail / inconclusive）、problems（比較できない・安全違反。1 つでもあれば fail）、
    regressions（前回より下がったタスク。判定には使わない診断）、profiles（プロファイル別の差・区間・判定）。"""
    problems = _comparability(current, previous, metric)
    regressions: list[str] = []
    per_profile: dict[str, dict[str, Any]] = {}
    ct, pt = current.get("meta", {}).get("tasks", {}), previous.get("meta", {}).get("tasks", {})
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
                regressions.append(f"{pid}/{tid}: {metric} {pv[metric]:.2f} → {cv[metric]:.2f}")
        ci = paired_bootstrap(diffs)
        per_profile[pid] = {"n_tasks": ci.n_tasks, "estimate": ci.estimate,
                            "ci": [ci.low, ci.high] if ci.low is not None else None,
                            "decision": decide(ci, margin, min_tasks)}
    decisions = {v["decision"] for v in per_profile.values()}
    decision = ("fail" if problems or "fail" in decisions else
                "inconclusive" if "inconclusive" in decisions or not decisions else "pass")
    return {"decision": decision, "metric": metric, "margin": margin, "min_tasks": min_tasks,
            "problems": problems, "regressions": regressions, "profiles": per_profile}
