"""トークン使用量と費用（解く側 solver と判定側 judge を分けて数える）。

単価は prices.json（公開 API 価格。出典 URL と取得日つき）から引く。載っていないモデルは費用「不明」（None）にし、推測しない。
トークンの定義の違い:
  - Claude: input_tokens はキャッシュを含まない。cache_read / cache_write（cache_creation）は別に数える
  - OpenAI（Codex）: input_tokens はキャッシュ読みを含む（cached_input_tokens は内数）。output_tokens は推論トークンを含む
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

TOKEN_KINDS = ("input", "cache_read", "cache_write", "output")
# 判定 1 回あたりのトークンの仮定（判定キャッシュに実測の履歴がないときだけ使う。上限寄りに置く）:
# CLI が足す固定の指示（system prompt など）をすべてキャッシュなしの入力として数え、出力は推論を含めて多めに取る
JUDGE_ASSUMED_OVERHEAD_TOKENS = 12_000
JUDGE_ASSUMED_OUTPUT_TOKENS = {"claude_code": 300, "codex": 1_500}
# 履歴のない解く側の 1 呼び出しの見積もり: タスクの max_budget_usd（CLI が 1 呼び出しに課す上限）があればそれ、なければこの既定。
# 開始前の --max-cost-usd の判定で $0 と数えないための、上限寄りの仮定（実測が残れば置き換わる）
DEFAULT_SOLVER_CALL_USD = 0.50
CHARS_PER_TOKEN = 1.0  # 日本語を含む判定入力は 1 文字 ≒ 1 トークン以下（上限寄り）


@lru_cache(maxsize=1)
def prices() -> dict[str, Any]:
    return json.loads((Path(__file__).with_name("prices.json")).read_text(encoding="utf-8"))


def resolve(model: str | None, family: str | None = None) -> tuple[str | None, dict[str, Any] | None]:
    """モデル名（opus のような別名・claude-opus-5-5 のような id・日付つきの id）を価格表の行に対応づける。不明なら (None, None)。"""
    m = (model or "").strip().lower()
    if not m:
        return None, None
    for mid, row in prices()["models"].items():
        if family and row.get("family") != family:
            continue
        if m == mid or m.startswith(mid) or m in row.get("aliases", []):
            return mid, row
    return None, None


def token_cost(tokens: dict[str, Any] | None, model: str | None, family: str) -> float | None:
    """公開価格で換算した費用（USD）。モデルの単価が分からない・トークンの記録がないなら None。"""
    _, row = resolve(model, family)
    if row is None or not tokens or all(tokens.get(k) is None for k in TOKEN_KINDS):
        return None
    t = {k: int(tokens.get(k) or 0) for k in TOKEN_KINDS}
    uncached = max(t["input"] - t["cache_read"], 0) if family == "codex" else t["input"]
    return (uncached * row["input"] + t["cache_read"] * row["cache_read"] + t["cache_write"] * row["cache_write"]
            + t["output"] * row["output"]) / 1e6


def empty_usage() -> dict[str, Any]:
    return {**{k: 0 for k in TOKEN_KINDS}, "cost_usd": 0.0, "cost_unknown": 0, "calls": 0}


def add_usage(acc: dict[str, Any], tokens: dict[str, Any] | None, cost: float | None) -> None:
    acc["calls"] += 1
    for k in TOKEN_KINDS:
        acc[k] += int((tokens or {}).get(k) or 0)
    if cost is None:
        acc["cost_unknown"] += 1
    else:
        acc["cost_usd"] += cost


def solver_cost(rec: Any, profile_model: str | None) -> tuple[float | None, str]:
    """試行（解く側）の費用と出どころ。公開価格で換算できればそれ（price_table）、できなければ CLI の推定（cli）、どちらもなければ不明。"""
    model = (rec.meta or {}).get("model") or profile_model
    c = token_cost(rec.tokens, model, rec.agent)
    if c is not None:
        return c, "price_table"
    if rec.cost_usd is not None:
        return rec.cost_usd, "cli"
    return None, "unknown"


def usage_summary(records: Iterable[Any], judge_usage: dict[str, Any] | None, profile_models: dict[str, str | None],
                  tier: str | None) -> dict[str, Any]:
    """summary.json の usage: solver / judge 別のトークン（入力・キャッシュ読み・キャッシュ書き・出力）と公開価格での費用、判定の割合。
    キャッシュから再利用した試行は、この実行では呼んでいないので数えない（from_cache_trials に件数だけ出す）。"""
    solver = empty_usage()
    sources: dict[str, int] = {}
    cached = 0
    for r in records:
        if getattr(r, "from_cache", False):
            cached += 1
            continue
        if not r.tokens and r.cost_usd is None:
            continue  # 起動しなかった試行（予算で止めた・中断）
        c, src = solver_cost(r, profile_models.get(r.profile_id))
        sources[src] = sources.get(src, 0) + 1
        add_usage(solver, r.tokens, c)
    judge = dict(judge_usage or empty_usage())
    total = solver["cost_usd"] + judge["cost_usd"]
    p = prices()
    return {
        "solver": {**solver, "cost_sources": sources, "from_cache_trials": cached},
        "judge": judge,
        "total_cost_usd": total,
        "judge_share": (judge["cost_usd"] / total) if total else None,
        "cost_complete": solver["cost_unknown"] == 0 and judge.get("cost_unknown", 0) == 0,
        "tier": tier,
        "prices": {"fetched_at": p["fetched_at"], "sources": p["sources"]},
    }


def solver_call_estimate(max_budget_usd: float | None) -> float:
    """履歴のない解く側 1 呼び出しの見積もり（上限寄り）。"""
    return float(max_budget_usd) if max_budget_usd else DEFAULT_SOLVER_CALL_USD


def judge_call_estimate(model: str | None, family: str, prompt_chars: int,
                        history: dict[str, Any] | None = None) -> tuple[float | None, str]:
    """判定 1 回の見積もり（費用, 根拠）。判定キャッシュの実測（history: そのモデルの 1 回あたりの平均トークン）があればそれ、
    なければ上限寄りの仮定（固定の指示 + 判定入力をすべてキャッシュなしの入力、出力は推論込みで多め）。"""
    if history:
        tok = {k: float(history.get(k) or 0) for k in TOKEN_KINDS}
        basis = f"実測 {history.get('n')} 回の平均"
    else:
        tok = {"input": JUDGE_ASSUMED_OVERHEAD_TOKENS + prompt_chars / CHARS_PER_TOKEN, "cache_read": 0, "cache_write": 0,
               "output": JUDGE_ASSUMED_OUTPUT_TOKENS.get(family, 1_500)}
        basis = "仮定（実測なし・上限寄り）"
    return token_cost(tok, model, family), basis


def fmt_usd(x: float | None) -> str:
    return "不明" if x is None else f"${x:.4f}"
