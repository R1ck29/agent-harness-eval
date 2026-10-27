"""指標の計算。

- 試行スコア   = 合格したグレーダーの重み付き割合（not_run は分母から除外）
- 試行の合格   = スコア 1.0 かつ 失敗扱いの異常終了なし
- pass@1       = タスク内の有効試行のうち合格した割合（全体はタスク平均）
- pass^k       = タスク内で k 回連続で合格する確率。有効試行 n 回・合格 c 回のとき C(c,k)/C(n,k)。
                 n = k なら「k 試行すべて合格なら 1、そうでなければ 0」と同じ。n < k のタスクは算出しない。
- Δ            = 比較プロファイル − 基準プロファイル（両方で値があるタスクだけで計算）
- 合格あたりのコスト = 総コスト ÷ 合格試行数（コスト不明の試行があれば算出しない）
- 異常終了（rate_limit / auth など）の試行は「無効」として集計から除外する
"""
from __future__ import annotations

from collections import Counter, defaultdict
from math import comb
from statistics import mean
from typing import Any, Iterable

from .record import TrialRecord


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
        tasks.setdefault(pid, {})[tid] = {
            "trials": len(rs), "valid": n, "passed": c, "excluded": len(rs) - n,
            "pass_at_1": (c / n) if n else None, "pass_hat_k": pass_hat_k(n, c, k),
            "mean_score": _mean(r.score for r in valid),
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


def deltas(summary: dict[str, Any], baseline: str) -> dict[str, dict[str, Any]]:
    """基準プロファイルとの差（同じエージェントに限らず計算する）。"""
    out: dict[str, dict[str, Any]] = {}
    tasks = summary["tasks"]
    if baseline not in tasks:
        return out
    base = tasks[baseline]
    for pid, tv in tasks.items():
        if pid == baseline:
            continue
        common = [t for t in tv if t in base]
        row: dict[str, Any] = {"common_tasks": len(common)}
        for m in ("pass_at_1", "pass_hat_k", "mean_score"):
            pairs = [(tv[t][m], base[t][m]) for t in common if tv[t][m] is not None and base[t][m] is not None]
            row[m] = mean(a - b for a, b in pairs) if pairs else None
        pa, pb = summary["profiles"][pid], summary["profiles"][baseline]
        row["cost_per_pass_usd"] = (pa["cost_per_pass_usd"] - pb["cost_per_pass_usd"]
                                    if pa["cost_per_pass_usd"] is not None and pb["cost_per_pass_usd"] is not None else None)
        row["per_task"] = {t: {m: (tv[t][m] - base[t][m]) if tv[t][m] is not None and base[t][m] is not None else None
                               for m in ("pass_at_1", "pass_hat_k")} for t in common}
        out[pid] = row
    return out


def gate(current: dict[str, Any], previous: dict[str, Any], metric: str = "pass_hat_k") -> list[str]:
    """前回より指標が下がった／比較できないタスクを列挙する（空ならゲート通過）。"""
    problems = []
    cm, pm = current.get("meta", {}), previous.get("meta", {})
    ct, pt = cm.get("tasks", {}), pm.get("tasks", {})
    uses_llm = any(t.get("llm", True) for t in [*ct.values(), *pt.values()])  # 古い結果は llm の有無が不明なので使う扱い
    if uses_llm and cm.get("options", {}).get("llm") != pm.get("options", {}).get("llm"):
        problems.append("LLM 判定の有無が前回と違うため比較できません（スモークとフルを比べていないか確認）")
    for pid, prev_tasks in previous["tasks"].items():
        cur_tasks = current["tasks"].get(pid)
        if cur_tasks is None:
            problems.append(f"{pid}: 今回の結果にプロファイルがありません")
            continue
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
            elif cv[metric] < pv[metric] - 1e-9:
                problems.append(f"{pid}/{tid}: {metric} {pv[metric]:.2f} → {cv[metric]:.2f}")
    return problems
