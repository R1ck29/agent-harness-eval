"""エージェント非依存の「正規化した試行レコード」。

Claude Code / Codex どちらの出力もこの形に変換してから採点・集計する。
値が取れない項目は None のまま（0 で埋めない）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 集計から除外する異常終了（評価対象の失敗ではなく、環境起因）
EXCLUDED_ERRORS = {"rate_limit", "overloaded", "auth", "network", "launch_failed", "harness_error", "judge_error",
                   "not_graded", "interrupted"}
# 失敗として数える異常終了


@dataclass
class ToolCall:
    name: str
    input: Any
    is_error: bool | None = None
    from_subagent: bool = False


@dataclass
class ParsedRun:
    """アダプタのパーサが返す中間結果。"""
    tool_calls: list[ToolCall] = field(default_factory=list)
    skill_calls: list[str] = field(default_factory=list)
    subagent_calls: list[str] = field(default_factory=list)
    hook_calls: list[dict[str, Any]] | None = None  # [{name, exit_code, outcome}]。記録していなければ None（Codex・hook_used なし）
    final_message: str | None = None
    cost_usd: float | None = None
    cost_source: str | None = None  # "client_estimate"（CLI の API 単価換算）/ "token_estimate"（pricing から計算）/ None（不明）
    duration_ms: int | None = None
    num_turns: int | None = None
    tokens: dict[str, int | None] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    skill_detection: str = "exact"  # "exact" / "heuristic"


@dataclass
class TrialRecord:
    run_id: str
    task_id: str
    profile_id: str
    agent: str
    trial: int
    cache_key: str
    task_hash: str
    profile_hash: str
    cli_version: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    skill_calls: list[str] = field(default_factory=list)
    subagent_calls: list[str] = field(default_factory=list)
    hook_calls: list[dict[str, Any]] | None = None  # [{name, exit_code, outcome}]。記録していなければ None（Codex・hook_used なし）
    final_message: str | None = None
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    cost_source: str | None = None
    judge_cost_usd: float | None = 0.0  # 判定を実行しなければ 0、実行して費用不明なら None
    duration_ms: int | None = None
    wall_ms: int | None = None
    num_turns: int | None = None
    tokens: dict[str, int | None] = field(default_factory=dict)
    grader_results: list[dict[str, Any]] = field(default_factory=list)
    score: float | None = None
    passed: bool = False
    # 比較用: 配布物がないと通らない判定（harness_only）を除いたスコアと合否。基準との Δ に使う。
    # 除いた後に判定が 1 つも残らなければ None（Δ に使わない）
    cmp_score: float | None = None
    cmp_passed: bool | None = None
    valid: bool = True
    error: dict[str, str] | None = None
    failure_tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    skill_detection: str = "exact"
    from_cache: bool = False
    trial_dir: str | None = None
    started_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrialRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
