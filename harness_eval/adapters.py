"""エージェント CLI ごとの「起動コマンドの組み立て」と「出力のパース」。

- Claude Code: `claude -p ... --output-format stream-json --verbose`
  （stream-json は --verbose 必須。v2.1.304 で確認）
- Codex: `codex exec --json ... -`（プロンプトは標準入力。0.163.0 で確認）

パーサは未知のイベントを無視する（CLI の版上げでイベントが増えても壊れないように）。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from .record import ParsedRun, ToolCall
from .spec import Profile, Task

# 隔離用に --settings で渡す設定。claude.ai アカウントのスキル/プラグイン同期を止め、
# 配布物以外のスキルが混入しないようにする（実測: 94 → 23 件。残りは Claude Code 同梱分）。
CLAUDE_ISOLATION_SETTINGS = {"syncClaudeAiSkills": False, "syncClaudeAiPlugins": False}


def _exe(profile_exe: list[str] | None, default: str) -> list[str]:
    if profile_exe:
        return list(profile_exe)
    found = shutil.which(default)
    return [found or default]


def _jsonl(lines: Iterable[str], warnings: list[str]) -> Iterable[dict[str, Any]]:
    bad = 0
    for line in lines:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(obj, dict):
            yield obj
    if bad:
        warnings.append(f"JSON として読めない行: {bad} 行")


def _classify_message(msg: str) -> str:
    m = msg.lower()
    if re.search(r"rate.?limit|\b429\b|too many requests", m):
        return "rate_limit"
    if re.search(r"overloaded|\b529\b", m):
        return "overloaded"
    if re.search(r"\b401\b|\b403\b|unauthori[sz]ed|not logged in|invalid api key|authentication|login required|credential", m):
        return "auth"
    return "agent_error"


def _status_type(status: Any) -> str | None:
    """API の HTTP ステータスから環境起因の異常終了を判定する（該当しなければ None）。"""
    if status == 429:
        return "rate_limit"
    if status == 529:
        return "overloaded"
    if status in (401, 403):
        return "auth"
    return None


class _Adapter:
    """共通部分。プロンプトはどちらの CLI にも標準入力で渡す（Claude Code 2.1.304 の -p、Codex の `-` で確認）。
    コマンドライン引数だと Windows の長さ制限や引用符の扱いで壊れうるため。"""
    command = ""

    def __init__(self, profile: Profile | None = None, executable: list[str] | None = None):
        self.exe = _exe(executable or (profile.executable if profile else None), self.command)

    def version_cmd(self) -> list[str]:
        return self.exe + ["--version"]


# ---------------------------------------------------------------- Claude Code
class ClaudeAdapter(_Adapter):
    agent = "claude_code"
    command = "claude"
    config_env = "CLAUDE_CONFIG_DIR"
    config_dirname = ".claude"

    def build_cmd(self, task: Task, profile: Profile, workdir: Path, extra_args: list[str] | None = None) -> list[str]:
        cmd = self.exe + ["-p", "--output-format", "stream-json", "--verbose",
                          "--max-turns", str(task.max_turns), "--no-session-persistence"]
        if task.uses_hooks:  # hook の実行記録は hook_used を使うタスクだけ取る（2.1.306 で確認したフラグ）
            cmd.append("--include-hook-events")
        if profile.model:
            cmd += ["--model", profile.model]
        if task.allow_tools:
            cmd += ["--allowedTools", ",".join(task.allow_tools)]
        if task.deny_tools:
            cmd += ["--disallowedTools", ",".join(task.deny_tools)]
        if profile.permission_mode:
            cmd += ["--permission-mode", profile.permission_mode]
        if task.max_budget_usd is not None:
            cmd += ["--max-budget-usd", str(task.max_budget_usd)]
        if profile.isolation_settings:
            cmd += ["--settings", json.dumps(CLAUDE_ISOLATION_SETTINGS)]
        return cmd + (profile.extra_args if extra_args is None else extra_args)

    def judge_cmd(self, model: str | None) -> list[str]:
        cmd = self.exe + ["-p", "--output-format", "json", "--max-turns", "1",
                          "--tools", "", "--no-session-persistence",
                          "--settings", json.dumps(CLAUDE_ISOLATION_SETTINGS)]
        return cmd + (["--model", model] if model else [])

    @staticmethod
    def parse_judge(stdout: str) -> tuple[str | None, float | None, str | None]:
        """(応答, 費用, 失敗理由)。is_error（未ログインなど）や success 以外は失敗理由を返す。"""
        for obj in _jsonl(stdout.splitlines(), []):
            if obj.get("type") == "result":
                cost = obj.get("total_cost_usd") if isinstance(obj.get("total_cost_usd"), (int, float)) else None
                if obj.get("is_error") or obj.get("subtype") not in (None, "success"):
                    st = _status_type(obj.get("api_error_status"))
                    return None, cost, f"{st or obj.get('subtype')}: {str(obj.get('result') or '')[:200]}"
                return obj.get("result"), cost, None
        return None, None, "result がありません"

    def parse(self, lines: Iterable[str], profile: Profile | None = None) -> ParsedRun:
        r = ParsedRun(hook_calls=[])
        pending: dict[str, ToolCall] = {}
        result: dict[str, Any] | None = None
        last_text: str | None = None
        retries: list[str] = []
        for e in _jsonl(lines, r.warnings):
            t, st = e.get("type"), e.get("subtype")
            if t == "system" and st == "init":
                r.meta.update({
                    "cli_version": e.get("claude_code_version"),
                    "model": e.get("model"),
                    "loaded_skills": e.get("skills") or [],
                    "loaded_plugins": [p.get("name") for p in (e.get("plugins") or []) if isinstance(p, dict)],
                    "mcp_servers": [f"{m.get('name')}:{m.get('status')}" for m in (e.get("mcp_servers") or []) if isinstance(m, dict)],
                    "loaded_agents": e.get("agents") or [],
                })
            elif t == "system" and st == "hook_response":  # --include-hook-events（2.1.306 で実測）
                r.hook_calls.append({"name": e.get("hook_name"), "exit_code": e.get("exit_code"),
                                     "outcome": e.get("outcome")})
            elif t == "system" and st == "api_retry":
                retries.append(str(e.get("error")))
            elif t == "assistant":
                parent = e.get("parent_tool_use_id")
                for b in (e.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                        tc = ToolCall(name=str(b.get("name")), input=inp, from_subagent=parent is not None)
                        r.tool_calls.append(tc)
                        if b.get("id"):
                            pending[b["id"]] = tc
                        if tc.name == "Skill" and inp.get("skill"):
                            r.skill_calls.append(str(inp["skill"]))
                        if tc.name in ("Agent", "Task"):  # 旧版は Task という名前
                            r.subagent_calls.append(str(inp.get("subagent_type") or "general-purpose"))
                    elif b.get("type") == "text" and parent is None:
                        last_text = b.get("text")
            elif t == "user":
                content = (e.get("message") or {}).get("content")
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                        pending[b["tool_use_id"]].is_error = bool(b.get("is_error"))
            elif t == "result":
                result = e
        if retries:
            r.meta["api_retries"] = retries
        if result is None:
            r.final_message = last_text
            r.error_type, r.error_message = "agent_crash", "result イベントがありません"
            return r
        r.duration_ms = result.get("duration_ms")
        r.num_turns = result.get("num_turns")
        if isinstance(result.get("total_cost_usd"), (int, float)):
            # CLI が API 単価で計算した推定額（請求額ではない。サブスクリプションでは呼び出しごとの請求はない）
            r.cost_usd, r.cost_source = float(result["total_cost_usd"]), "client_estimate"
        u = result.get("usage") or {}
        r.tokens = {"input": u.get("input_tokens"), "output": u.get("output_tokens"),
                    "cache_read": u.get("cache_read_input_tokens"),
                    "cache_write": u.get("cache_creation_input_tokens")}
        r.meta["terminal_reason"] = result.get("terminal_reason")
        st = result.get("subtype")
        r.final_message = result.get("result") if isinstance(result.get("result"), str) else last_text
        status = result.get("api_error_status")
        if st == "error_max_turns":
            r.error_type, r.error_message = "max_turns", "max_turns に到達"
        elif st == "error_max_budget_usd":
            r.error_type, r.error_message = "budget_exceeded", "max_budget_usd に到達"
        elif st and st != "success":
            msg = "; ".join(map(str, result.get("errors") or [])) or str(st)
            r.error_type, r.error_message = _status_type(status) or _classify_message(msg), msg[:500]
        elif result.get("is_error"):
            msg = str(result.get("result") or "is_error=true")
            r.error_type, r.error_message = _status_type(status) or _classify_message(msg), msg[:500]
        return r


# ---------------------------------------------------------------- Codex
# Codex は SKILL.md をシェルで読む。読んだコマンドからスキル名を推定する（厳密ではない）。
_SKILL_RE = re.compile(r"skills[/\\]+([A-Za-z0-9_.-]+)[/\\]+SKILL\.md")


class CodexAdapter(_Adapter):
    agent = "codex"
    command = "codex"
    config_env = "CODEX_HOME"
    config_dirname = ".codex"

    def build_cmd(self, task: Task, profile: Profile, workdir: Path, extra_args: list[str] | None = None) -> list[str]:
        cmd = self.exe + ["exec", "--json", "--skip-git-repo-check", "--ephemeral",
                          "-C", str(workdir), "-s", profile.codex_sandbox,
                          # 一時ホームの hooks は未信頼のため、これがないと警告なしで実行されない（0.163.0 で実測）。
                          # 普段は一度信頼すれば実行されるので、その状態を再現する
                          "--dangerously-bypass-hook-trust"]
        if profile.model:
            cmd += ["-m", profile.model]
        return cmd + (profile.extra_args if extra_args is None else extra_args) + ["-"]

    def judge_cmd(self, model: str | None) -> list[str]:
        cmd = self.exe + ["exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only"]
        return cmd + (["-m", model] if model else []) + ["-"]

    def parse_judge(self, stdout: str) -> tuple[str | None, float | None, str | None]:
        r = self.parse(stdout.splitlines())
        return r.final_message, None, (f"{r.error_type}: {r.error_message}" if r.error_type else None)

    def parse(self, lines: Iterable[str], profile: Profile | None = None) -> ParsedRun:
        r = ParsedRun(skill_detection="heuristic")
        tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning_output": 0}
        completed = failed = progressed = False
        reconnects = 0
        for e in _jsonl(lines, r.warnings):
            t = e.get("type")
            if t == "item.completed" and ((e.get("item") or {}).get("type") or "error") != "error":
                progressed = True
            if t == "thread.started":
                r.meta["thread_id"] = e.get("thread_id")
            elif t == "item.completed":
                item = e.get("item") or {}
                it = item.get("type") or item.get("item_type")  # 初期版は item_type
                if it == "agent_message":
                    r.final_message = item.get("text")
                elif it == "command_execution":
                    cmd = str(item.get("command", ""))
                    st = item.get("status")
                    ec = item.get("exit_code")
                    r.tool_calls.append(ToolCall("shell", {"command": cmd},
                                                 is_error=st in ("failed", "declined") or (ec not in (None, 0))))
                    r.skill_calls += _SKILL_RE.findall(cmd)
                elif it == "file_change":
                    r.tool_calls.append(ToolCall("file_change", {"changes": item.get("changes")},
                                                 is_error=item.get("status") == "failed"))
                elif it == "mcp_tool_call":
                    # Claude Code と同じ mcp__<server>__<tool> 形式にそろえる
                    r.tool_calls.append(ToolCall(f"mcp__{item.get('server')}__{item.get('tool')}",
                                                 item.get("arguments") or {},
                                                 is_error=item.get("status") == "failed" or bool(item.get("error"))))
                elif it == "collab_tool_call":
                    tool = str(item.get("tool"))
                    r.tool_calls.append(ToolCall(f"collab:{tool}", {"prompt": item.get("prompt")},
                                                 is_error=item.get("status") == "failed"))
                    if tool == "spawn_agent":
                        r.subagent_calls.append("spawn_agent")
                elif it == "web_search":
                    r.tool_calls.append(ToolCall("web_search", {"query": item.get("query")}))
                elif it == "error" and "--dangerously-bypass-hook-trust" not in str(item.get("message")):
                    r.warnings.append(f"item error: {str(item.get('message'))[:200]}")  # 上のフラグの既知の通知は記録しない
            elif t == "turn.completed":
                completed = True
                u = e.get("usage") or {}
                tok["input"] += int(u.get("input_tokens") or 0)
                tok["output"] += int(u.get("output_tokens") or 0)
                tok["cache_read"] += int(u.get("cached_input_tokens") or 0)
                tok["cache_write"] += int(u.get("cache_write_input_tokens") or 0)
                tok["reasoning_output"] += int(u.get("reasoning_output_tokens") or 0)
            elif t == "turn.failed":
                failed = True
                msg = str((e.get("error") or {}).get("message", "turn.failed"))
                r.error_type, r.error_message = _classify_message(msg), msg[:500]
            elif t == "error":  # 再接続などの通知。致命的ではない（実測）
                r.warnings.append(f"error event: {str(e.get('message'))[:200]}")
                reconnects += str(e.get("message", "")).startswith("Reconnecting")
        # ネットワーク不通では再接続を続けるだけで何も進まない（実測）。環境起因として除外できるよう印を付ける
        r.meta["network_stalled"] = reconnects > 0 and not (completed or failed or progressed)
        # num_turns は付けない: codex exec の turn は 1 回の実行に 1 つで、Claude Code のターン数と比べられないため
        if completed:
            r.tokens = dict(tok)
            pr = profile.pricing if profile else None
            if pr:
                # OpenAI API の usage 定義（cached は input の内数）を前提にした概算
                uncached = max(tok["input"] - tok["cache_read"], 0)
                r.cost_usd = (uncached * pr.get("input_per_mtok", 0)
                              + tok["cache_read"] * pr.get("cached_input_per_mtok", pr.get("input_per_mtok", 0))
                              + tok["output"] * pr.get("output_per_mtok", 0)) / 1e6
                r.cost_source = "token_estimate"
        if not completed and not failed:
            r.error_type, r.error_message = "agent_crash", "turn.completed / turn.failed がありません"
        r.skill_calls = list(dict.fromkeys(r.skill_calls))
        return r


# 起動フラグ・出力形式を実機で確かめた版（これより古い CLI は未確認。doctor が知らせる）
VERIFIED_VERSIONS = {"claude_code": (2, 1, 304), "codex": (0, 163, 0)}


def older_than_verified(agent: str, version: str | None) -> bool | None:
    """`claude --version` / `codex --version` の出力が確認済みの版より古いか（版を読めなければ None）。"""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups()) < VERIFIED_VERSIONS[agent]


def get_adapter(agent: str, profile: Profile | None = None, executable: list[str] | None = None):
    if agent == "claude_code":
        return ClaudeAdapter(profile, executable)
    if agent == "codex":
        return CodexAdapter(profile, executable)
    raise ValueError(agent)


def classify_stderr(stderr: str) -> str | None:
    """result が出ないまま落ちたときに stderr から原因を推定する。"""
    if not stderr.strip():
        return None
    c = _classify_message(stderr)
    return c if c != "agent_error" else None
