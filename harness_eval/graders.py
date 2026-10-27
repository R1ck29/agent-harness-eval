"""グレーダー（採点器）。

status:
  pass / fail          … 通常の判定
  unknown              … LLM 判定が「根拠不足」と答えた（不合格扱い）
  not_run              … --no-llm で LLM 判定を実行しなかった（スコアの分母から除外）
  skipped_prior_fail   … コード判定が既に不合格なので LLM 判定を省いた（不合格扱い。
                          試行の合否は変わらないのでトークン節約のため省略する）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .isolation import fill
from .record import TrialRecord
from .spec import Grader

JUDGE_PROMPT = """あなたは厳格な採点者です。以下の「評価観点」だけに基づき、「対象」を判定してください。
外部知識で補わず、対象に書かれていることだけで判断します。根拠が不足していれば UNKNOWN とします。

# 評価観点
{criteria}

# 対象（{focus}）
<<<
{content}
>>>

1 行目に PASS / FAIL / UNKNOWN のいずれか 1 語だけを書き、2 行目に理由を 1 文で書いてください。"""


@dataclass
class JudgeConfig:
    enabled: bool = True
    run: Callable[[str], tuple[str | None, float | None]] | None = None  # prompt -> (text, cost)


def _safe_path(workdir: Path, rel: str) -> Path:
    p = (workdir / rel).resolve()
    if workdir.resolve() not in p.parents and p != workdir.resolve():
        raise ValueError(f"作業ディレクトリ外のパス: {rel}")
    return p


def _count_check(n: int, params: dict[str, Any]) -> tuple[bool, str]:
    lo = params.get("min", 1 if "max" not in params else 0)
    hi = params.get("max")
    ok = n >= lo and (hi is None or n <= hi)
    rng = f">= {lo}" + (f" かつ <= {hi}" if hi is not None else "")
    return ok, f"{n} 回（期待: {rng}）"


def grade_one(g: Grader, rec: TrialRecord, workdir: Path, task_dir: Path | None = None) -> tuple[bool, str]:
    t, p = g.type, g.params
    if t == "file_exists":
        f = _safe_path(workdir, p["path"])
        return f.is_file(), f"{p['path']} {'あり' if f.is_file() else 'なし'}"
    if t == "file_contains":
        f = _safe_path(workdir, p["path"])
        if not f.is_file():
            return False, f"{p['path']} がありません"
        text = f.read_text(encoding="utf-8", errors="replace")
        hit = re.search(p["pattern"], text, re.MULTILINE) is not None
        return hit, f"/{p['pattern']}/ {'一致' if hit else '不一致'}"
    if t == "regex":
        text = rec.final_message or ""
        hit = re.search(p["pattern"], text, re.MULTILINE) is not None
        return hit, f"最終応答が /{p['pattern']}/ に{'一致' if hit else '不一致'}"
    if t == "tool_used":
        name_re = re.compile(p["tool"])
        inp_re = re.compile(p["input_match"]) if p.get("input_match") else None
        n = 0
        for c in rec.tool_calls:
            if not p.get("include_subagents", True) and c.get("from_subagent"):
                continue
            if not name_re.fullmatch(c["name"]):
                continue
            if inp_re and not inp_re.search(json.dumps(c.get("input"), ensure_ascii=False)):
                continue
            n += 1
        return _count_check(n, p)
    if t == "skill_used":
        n = sum(1 for s in rec.skill_calls if s == p["skill"] or s.endswith(":" + p["skill"]))
        ok, why = _count_check(n, p)
        return ok, why + ("（推定）" if rec.skill_detection == "heuristic" else "")
    if t == "subagent_used":
        n = sum(1 for s in rec.subagent_calls if "name" not in p or s == p["name"] or s.endswith(":" + str(p["name"])))
        return _count_check(n, p)
    if t == "hook_used":
        rx = re.compile(str(p["hook"]))
        n = sum(1 for h in rec.hook_calls or [] if rx.fullmatch(str(h.get("name")))
                and ("outcome" not in p or h.get("outcome") == p["outcome"]))
        return _count_check(n, p)
    if t == "command":
        # 置換した値はシェル用に引用される（空白を含むパスでも壊れない）。YAML 側では引用符で囲まないこと
        ph = {"python": sys.executable, "workdir": str(workdir)}
        if task_dir is not None:
            ph["task_dir"] = str(task_dir)
        cmd = fill(str(p["run"]), ph, quote=True)
        rec_path = workdir.parent / "record_for_grader.json"
        rec_path.write_text(json.dumps(rec.to_dict(), ensure_ascii=False), encoding="utf-8")
        env = {**os.environ, "TRIAL_WORKDIR": str(workdir), "TRIAL_RECORD": str(rec_path)}
        try:
            r = subprocess.run(cmd, shell=True, cwd=workdir, env=env, capture_output=True, text=True,
                               timeout=int(p.get("timeout_s", 120)), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, "採点コマンドがタイムアウト"
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
        return r.returncode == 0, f"exit={r.returncode} {tail[0][:200]}"
    raise ValueError(t)


def _judge(g: Grader, rec: TrialRecord, workdir: Path, judge: JudgeConfig) -> tuple[str, str, float | None]:
    focus = g.params.get("focus", "final")
    max_chars = int(g.params.get("max_chars", 20000))
    if focus == "final":
        content = rec.final_message or ""
    else:
        f = _safe_path(workdir, focus)
        if not f.is_file():
            return "fail", f"{focus} がありません", 0.0  # 判定は呼んでいない
        content = f.read_text(encoding="utf-8", errors="replace")
    if len(content) > max_chars:
        content = content[:max_chars] + "\n…(以下省略)"
    assert judge.run is not None
    text, cost = judge.run(JUDGE_PROMPT.format(criteria=g.params["criteria"], focus=focus, content=content))
    if not text:
        return "unknown", "判定モデルの応答なし", cost
    first = text.strip().splitlines()[0].strip().upper().strip("*`# ")
    reason = " ".join(text.strip().splitlines()[1:2])[:200]
    if first.startswith("PASS"):
        return "pass", reason, cost
    if first.startswith("FAIL"):
        return "fail", reason, cost
    return "unknown", reason or text[:200], cost


class JudgeUnavailable(Exception):
    """判定モデルを呼べなかった（認証・起動・タイムアウトなど）。試行は集計から除外される。"""


def grade_all(graders: list[Grader], rec: TrialRecord, workdir: Path, judge: JudgeConfig,
              task_dir: Path | None = None) -> None:
    """rec.grader_results / score / judge_cost_usd を埋める。"""
    results: list[dict[str, Any]] = []
    for g in graders:
        if g.type == "llm":
            results.append(None)  # type: ignore[arg-type]
            continue
        if g.type == "hook_used" and rec.hook_calls is None:  # Codex の --json には hooks の実行記録が出ない（0.163.0 で実測）
            results.append({"label": g.label(), "type": g.type, "kind": g.kind, "status": "not_run",
                            "reason": "hooks の実行記録がないため判定できません（Codex）", "weight": g.weight, "category": g.category})
            continue
        try:
            ok, why = grade_one(g, rec, workdir, task_dir)
            if g.negate:
                ok = not ok
            status = "pass" if ok else "fail"
        except Exception as e:  # 採点器の不具合は不合格として理由を残す
            status, why = "fail", f"採点エラー: {e}"
        results.append({"label": g.label(), "type": g.type, "kind": g.kind, "status": status,
                        "reason": why, "weight": g.weight, "category": g.category})
    code_ok = all(r is None or r["status"] in ("pass", "not_run") for r in results) and rec.error is None
    jcost = 0.0
    jcost_known = True
    for i, g in enumerate(graders):
        if g.type != "llm":
            continue
        base = {"label": g.label(), "type": g.type, "kind": g.kind, "weight": g.weight, "category": g.category}
        if not judge.enabled or judge.run is None:
            results[i] = {**base, "status": "not_run", "reason": "--no-llm"}
        elif not code_ok:
            results[i] = {**base, "status": "skipped_prior_fail", "reason": "他の判定が不合格のため省略"}
        else:
            try:
                st, why, cost = _judge(g, rec, workdir, judge)
            except JudgeUnavailable:
                raise
            except Exception as e:
                st, why, cost = "unknown", f"判定エラー: {e}", 0.0  # 判定呼び出し前後の不具合（呼び出し失敗は JudgeUnavailable）
            if g.negate and st in ("pass", "fail"):
                st = "fail" if st == "pass" else "pass"
            if cost is None:
                jcost_known = False
            else:
                jcost += cost
            results[i] = {**base, "status": st, "reason": why}
    rec.grader_results = results
    # 実行しなかった判定（not_run・skipped_prior_fail）は分母に入れない
    scored = [r for r in results if r["status"] not in ("not_run", "skipped_prior_fail")]
    total = sum(r["weight"] for r in scored)
    rec.score = (sum(r["weight"] for r in scored if r["status"] == "pass") / total) if total else None
    # 判定を実行しなければ 0、実行して費用が分からなければ None（集計で「不明」として扱う）
    rec.judge_cost_usd = jcost if jcost_known else None
