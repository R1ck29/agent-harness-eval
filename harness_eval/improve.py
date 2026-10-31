"""改善役（improve）: train の失敗だけを読ませた claude -p に、配布物の変更項目を作らせ、候補の配布物とプロファイルを書き出す。

流れ: 入力を確かめる（train だけの実行・copy で入れる配布物）→ OS の一時フォルダに材料（現行の配布物の写しと、失敗した試行の
記録の写し）を置く → 読み取りの隔離を自己確認（mockapi）→ 改善役を読み取りだけで起動（作業フォルダの外は読めず、holdout の
結果フォルダ・タスク定義・この実行のフォルダは deny）→ 変更項目を適用 → 事前検査 → 通れば dev 用・holdout 用・スキル比較用の
プロファイルを書き出す。材料（依頼文・トランスクリプトを含む）は最後に消す。improve.json には依頼文もトランスクリプトも書かない。
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .adapters import _classify_message, _status_type
from .candidate import (KINDS, OPS, apply_deltas, changed_skills, materialize, prechecks, profile_raw, write_profile,
                        write_skill_variants)
from .isolation import TrialPaths, ancestor_instructions, build_env, copy_credentials, make_trial_paths, run_process
from .mockapi import improver_cmd, init_problem, parse_stream, read_isolation_check, special_chars, unusable_path
from .record import TrialRecord
from .runner import load_run
from .spec import Profile, SpecError, Task, load_profile

MAX_DELTAS = 8
SCHEMA = {
    "type": "object",
    "required": ["deltas", "notes"],
    "properties": {
        "deltas": {"type": "array", "maxItems": MAX_DELTAS, "items": {
            "type": "object", "required": ["id", "kind", "file", "op", "old", "new", "reason", "fixes"],
            "properties": {"id": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
                           "file": {"type": "string"}, "op": {"type": "string", "enum": list(OPS)},
                           "old": {"type": "string"}, "new": {"type": "string"}, "reason": {"type": "string"},
                           "fixes": {"type": "array", "items": {"type": "string"}}}}},
        "notes": {"type": "string"},
    },
}

PROMPT = """あなたは、多くの利用者に配る Claude Code の配布物（設定ディレクトリに入る CLAUDE.md・rules/・skills/）を改善する担当です。
作業フォルダの中だけを読めます（書き込みはできません。変更は最後に構造化出力で返します）。
- harness/ : 現行の配布物（設定ディレクトリに入ったときの形）
- failures/<タスク id>/task.md : 現行の配布物で失敗した練習用（train）タスクの依頼
- failures/<タスク id>/<試行>/result.json : 採点の結果・失敗の分類・最終応答・ツールの呼び出し
- failures/<タスク id>/<試行>/transcript.jsonl : 試行の記録（ある場合）
{previous}
失敗の原因を読み、配布物への変更項目を出してください。守ること:
- 多くの依頼に通じる変更にする。特定のタスクの名前・ファイル名・列名・数値・依頼の文をそのまま書かない。採用するかは、あなたが
  見ていない別のタスクで決めるので、特定のタスクだけに効く変更は採用されない
- kind: INV = 作業手順（確認・検証・形式の守り方など、業務によらない手順）／ DOM = 業務知識（その業務の用語・データの扱い）
- file は harness/ からの相対パスで、Markdown（.md）だけ（例: CLAUDE.md、skills/<名前>/SKILL.md）。スクリプト・設定・hooks は変えない
- 毎回読み込まれる指示（CLAUDE.md・AGENTS.md・rules/、そこから @ で読み込むファイル、agents/ の説明）は合計で {max_growth} 文字までしか
  増やせない。足すなら既存の文を置き換えて短くするか、必要なときだけ読まれるスキル（skills/<名前>/SKILL.md。先頭に --- で囲んだ
  name と description）に入れる
- op: add = ファイルの末尾に new を追記（ファイルがなければ作る）／ replace = old をちょうど 1 回含む箇所を new に置き換える／
  remove = old を消す。old は harness/ のファイルから一字一句そのまま写す
- fixes: その項目で直ると予想する失敗タスクの id（下の一覧から）。予想は次の回で当たったかを確かめる
- 採点の方法や採点スクリプトに合わせた変更はしない
- 項目は効果の大きいものから {max_deltas} 個まで。notes には全体の見立てを短く書く

失敗した train タスク: {tasks}
"""

OTEL_PROMPT_VARS = ["OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_DETAILS"]  # 依頼文・記録を読ませるので、テレメトリへの記録は止める
SCRIPT_RE = re.compile(r"[\w./\\-]+\.(?:py|sh|ps1|js)")  # command グレーダーの採点スクリプト（spec.Task.content_hash と同じ見つけ方）


@dataclass
class ImproveOptions:
    run_dir: Path
    profile: Profile          # 改善する配布物（現行）
    tasks: list[Task]         # タスク定義（train の依頼文・採点スクリプト名・読ませない場所を知るため。改善役には見せない）
    holdout_outs: list[Path]  # holdout 専用の --out（改善役に読ませない）
    out: Path
    exe: list[str]            # 改善役の claude
    auth: Profile             # 改善役の認証・環境変数を借りる claude_code のプロファイル
    model: str | None = "sonnet"
    candidate_id: str = "candidate"
    max_growth: int = 0
    previous: Path | None = None
    max_turns: int = 40
    timeout_s: int = 1800


def train_failures(run_dir: Path, profile: Profile) -> tuple[dict[str, Any], dict[str, list[TrialRecord]], list[TrialRecord]]:
    """(実行の meta, タスク id → 失敗した試行, この配布物の全試行)。train だけの実行で、この配布物の結果であることを確かめる。"""
    recs, summary = load_run(run_dir)
    meta = summary.get("meta", {})
    other = sorted(t for t, v in meta.get("tasks", {}).items() if v.get("split") != "train")
    if other:
        raise SpecError(f"{run_dir}: train 以外のタスクを含む実行です（{other[:5]}）。改善役には train だけの実行（run --split train）を渡します")
    pm = meta.get("profiles", {}).get(profile.id)
    if pm is None:
        raise SpecError(f"{run_dir}: プロファイル {profile.id} の結果がありません")
    if pm.get("harness_hash") and pm["harness_hash"] != profile.harness_hash():
        raise SpecError(f"{profile.path}: 実行のあとで配布物が変わっています（実行時 {pm['harness_hash']}、今 {profile.harness_hash()}）。"
                        "今の配布物で train を実行し直してください")
    mine = [r for r in recs if r.profile_id == profile.id]
    failed: dict[str, list[TrialRecord]] = {}
    for r in mine:
        if r.valid and not r.passed:
            failed.setdefault(r.task_id, []).append(r)
    return meta, failed, mine


def grader_files(tasks: list[Task]) -> set[str]:
    """command グレーダーが使う採点スクリプトのファイル名。"""
    return {Path(tok).name for t in tasks for g in t.graders if g.type == "command"
            for tok in SCRIPT_RE.findall(str(g.params.get("run", "")))}


def deny_targets(run_dir: Path, holdout_outs: list[Path], tasks: list[Task], work: Path) -> list[Path]:
    """改善役に読ませない場所: holdout の結果フォルダ・タスク定義（タスクのフォルダとその親）・フィクスチャ・この実行の結果フォルダ。
    holdout が作業フォルダを含むなら止める。作業フォルダを含むそれ以外の場所（ホームなど）は外す（含めると材料まで読めなくなる。
    作業フォルダの外はどのみち読めない）。"""
    w = work.resolve()
    for h in holdout_outs:
        if w.is_relative_to(Path(h).resolve()):
            raise SpecError(f"改善役の作業フォルダ {w} が holdout の場所 {h} の中にあります")
    cands = [*holdout_outs, run_dir, run_dir.parent]
    for t in tasks:
        cands += [t.path.parent, t.path.parent.parent] + ([t.fixture] if t.fixture else [])
    out: list[Path] = []
    for p in cands:
        p = Path(p).resolve()
        if p not in out and not w.is_relative_to(p):
            out.append(p)
    return out


def _trial_view(r: TrialRecord) -> dict[str, Any]:
    """改善役に見せる 1 試行の記録（費用・キャッシュのキーなど、改善に関係のない値は外す）。"""
    calls = [{"name": c.get("name"), "input": json.dumps(c.get("input"), ensure_ascii=False)[:500], "is_error": c.get("is_error")}
             for c in r.tool_calls[:80]]
    return {"task": r.task_id, "trial": r.trial, "score": r.score, "failure_tags": r.failure_tags, "error": r.error,
            "graders": [{k: g.get(k) for k in ("label", "type", "status", "category", "reason")} for g in r.grader_results],
            "final_message": r.final_message, "tool_calls": calls, "skill_calls": r.skill_calls,
            "files_created": r.files_created, "files_modified": r.files_modified, "num_turns": r.num_turns}


def forbidden_marks(holdout_outs: list[Path], tasks: list[Task], run_dir: Path) -> list[str]:
    """train の記録に出ていたら、その記録は渡さない目印: holdout の場所・タスク定義のフォルダ・train 以外のタスク id。
    評価のエージェントは Read や Bash で外も読めるので、train の試行中に holdout などを読んで記録に残した可能性を除く。
    結果フォルダを含む場所（記録のパスに普通に出る）は目印にしない。"""
    run = run_dir.resolve()
    marks = set()
    for p in [Path(h) for h in holdout_outs] + [t.path.parent for t in tasks]:
        if not run.is_relative_to(p.resolve()):  # 書き方の違う同じ場所（/var と /private/var・相対パス）も目印にする
            marks |= {str(p), str(p.absolute()), str(p.resolve()), p.resolve().as_posix()}
    return sorted(marks | {t.id for t in tasks if t.split != "train"})


def _clean(text: str, marks: list[str]) -> bool:
    return not any(m in text for m in marks)


def write_material(work: Path, profile: Profile, tasks: dict[str, Task], failed: dict[str, list[TrialRecord]],
                   previous: dict[str, Any] | None, marks: list[str] = ()) -> int:
    """改善役の作業フォルダに、読ませてよいものの写しだけを置く。戻り値は目印を含むため渡さなかった記録の数。"""
    skipped = 0
    materialize(profile, work / "harness")
    for tid, recs in sorted(failed.items()):
        d = work / "failures" / tid
        d.mkdir(parents=True)
        t = tasks[tid]
        (d / "task.md").write_text(f"# {tid}\n\n{t.description}\n\n## 依頼\n\n{t.prompt}\n", encoding="utf-8")
        for r in recs:
            td = d / str(r.trial)
            td.mkdir()
            view = json.dumps(_trial_view(r), ensure_ascii=False, indent=2)
            if _clean(view, marks):
                (td / "result.json").write_text(view, encoding="utf-8")
            else:
                skipped += 1
            src = Path(r.trial_dir) / "stdout.jsonl" if r.trial_dir else None
            if src and src.is_file():
                text = src.read_text(encoding="utf-8", errors="replace")
                if _clean(text, marks):
                    (td / "transcript.jsonl").write_text(text, encoding="utf-8")
                else:
                    skipped += 1
    if previous is not None:
        (work / "previous.json").write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
    return skipped


def check_predictions(prev: dict[str, Any], recs: list[TrialRecord], meta: dict[str, Any], profile_id: str) -> dict[str, Any]:
    """前回の改善役の予想（直るはずのタスク）が、この実行で当たったか。直った = 有効な試行がすべて合格。"""
    by_task: dict[str, list[TrialRecord]] = {}
    for r in recs:
        if r.valid:
            by_task.setdefault(r.task_id, []).append(r)
    predicted = sorted(prev.get("predictions") or {})
    fixed = [t for t in predicted if by_task.get(t) and all(r.passed for r in by_task[t])]
    missed = [t for t in predicted if by_task.get(t) and t not in fixed]
    before = (prev.get("candidate") or {}).get("harness_hash")
    same = bool(before) and before == meta.get("profiles", {}).get(profile_id, {}).get("harness_hash")
    return {"predicted": predicted, "fixed": fixed, "not_fixed": missed, "not_run": [t for t in predicted if t not in by_task],
            "hit_rate": len(fixed) / (len(fixed) + len(missed)) if fixed or missed else None, "same_harness": same,
            "deltas": [{k: d.get(k) for k in ("id", "kind", "file", "reason", "fixes")} for d in prev.get("deltas") or []]}


def run_improver(o: ImproveOptions, tp: TrialPaths, prompt: str, deny: list[Path],
                 env: dict[str, str]) -> tuple[dict[str, Any] | None, float | None, str, int]:
    """改善役を 1 回起動する。(構造化出力, 費用, 失敗理由, 権限で止められた呼び出しの数)。"""
    cmd = improver_cmd(o.exe, o.model, deny, o.max_turns, ["--json-schema", json.dumps(SCHEMA)])
    pr = run_process(cmd, tp.workdir, env, prompt, tp.stdout, tp.stderr, o.timeout_s)
    if pr.launch_error or pr.timed_out:
        return None, None, "改善役を起動できないか、時間内に終わりませんでした", 0
    init, res = parse_stream(tp.stdout.read_text(encoding="utf-8", errors="replace") if tp.stdout.exists() else "")
    cost = res.get("total_cost_usd") if res and isinstance(res.get("total_cost_usd"), (int, float)) else None
    problem = init_problem(init)
    if problem:  # 前提と違う起動だった → 結果は使わない（読めてはいけないものを読めた可能性がある）
        return None, cost, f"改善役の起動が前提と違うため結果を捨てました: {problem}", 0
    if res is None:
        return None, cost, "改善役の応答がありません", 0
    denials = len(res.get("permission_denials") or [])
    if res.get("is_error") or not isinstance(res.get("structured_output"), dict):
        kind = _status_type(res.get("api_error_status")) or _classify_message(str(res.get("result") or ""))
        return None, cost, f"改善役の失敗（{kind if res.get('is_error') else '構造化出力なし'}）", denials
    return res["structured_output"], cost, "", denials


def improve(o: ImproveOptions, log: Callable[[str], None], spend: Callable[[float | None], None]) -> tuple[int, dict[str, Any]]:
    """戻り値: (終了コード, improve.json の中身)。0 候補あり／1 事前検査で見送り／2 止めた（隔離を確かめられない・改善役の失敗）／
    3 材料なし（失敗した train の試行がない）。"""
    meta, failed, mine = train_failures(o.run_dir, o.profile)
    if o.out.exists() and any(o.out.iterdir()):
        raise SpecError(f"{o.out} は空ではありません（候補ごとに新しいフォルダを指定してください）")
    report: dict[str, Any] = {
        "tool_version": __version__, "created_at": datetime.now(timezone.utc).isoformat(),
        "run": {"run_id": meta.get("run_id"), "path": str(o.run_dir.resolve())},
        "profile": {"id": o.profile.id, "harness_hash": o.profile.harness_hash()}, "model": o.model,
        "failed_tasks": {t: len(rs) for t, rs in sorted(failed.items())}}
    previous = None
    if o.previous:
        try:
            prev = json.loads(Path(o.previous).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SpecError(f"--previous を読めません: {e}") from e
        if not isinstance(prev, dict):
            raise SpecError(f"--previous は improve.json ではありません: {o.previous}")
        previous = report["previous"] = check_predictions(prev, mine, meta, o.profile.id)
    if not failed:
        invalid = sum(1 for r in mine if not r.valid)
        log("この実行に失敗した train の試行がありません（改善の材料がないので、改善役は呼びません）"
            + (f"。環境起因で除外された試行が {invalid} 件あります（実行し直すと材料が出るかもしれません）" if invalid else ""))
        _write_report(o.out, report)
        return 3, report
    tasks = {t.id: t for t in o.tasks}
    for tid in sorted(failed):
        t = tasks.get(tid)
        if t is None:
            raise SpecError(f"--tasks に失敗したタスク {tid} の定義がありません")
        if t.split != "train" or t.content_hash() != meta["tasks"][tid].get("hash"):
            raise SpecError(f"{t.path}: 実行のあとでタスクの分割か中身が変わっています（今の split: {t.split}）。今の定義で train を実行し直してください")
    root = Path(tempfile.mkdtemp(prefix="harness-eval-improve-")).resolve()
    try:
        tp = make_trial_paths(root / "agent", ".claude")
        leaks = ancestor_instructions(tp.workdir)
        if leaks:
            raise SpecError(f"一時フォルダの上に指示ファイルがあり、改善役に読み込まれます: {leaks}")
        deny = deny_targets(o.run_dir, o.holdout_outs, o.tasks, tp.workdir)
        bad = [str(p) for p in deny if unusable_path(p)]
        if bad:
            raise SpecError(f"読ませない場所のパスに \\ が含まれ、権限設定で止められません: {bad}（フォルダ名を変えてください）")
        report["skipped_material"] = write_material(tp.workdir, o.profile, tasks, failed, previous,
                                                    forbidden_marks(o.holdout_outs, o.tasks, o.run_dir))
        if report["skipped_material"]:
            log(f"holdout の場所・タスク定義・train 以外のタスク id を含む記録 {report['skipped_material']} 件は渡しません")
        auth = replace(o.auth, env_unset=list(o.auth.env_unset) + OTEL_PROMPT_VARS)
        env = build_env(auth, tp, "CLAUDE_CONFIG_DIR")
        for k in OTEL_PROMPT_VARS:  # プロファイルの env で付けていても外す
            env.pop(k, None)
        state, why = read_isolation_check(o.exe, o.model, env, special=special_chars(deny))
        report["isolation_check"] = {"state": state, "message": why}
        log(f"読み取りの隔離の自己確認: {state}（{why}）")
        if state != "ok":
            _write_report(o.out, report)
            return 2, report
        if auth.copy_credentials:
            copy_credentials("claude_code", tp)
        prompt = PROMPT.format(tasks=", ".join(sorted(failed)), max_growth=o.max_growth, max_deltas=MAX_DELTAS,
                               previous="- previous.json : 前回の改善の予想と、それが当たったか\n" if previous else "")
        log(f"改善役を起動します（失敗した train タスク {len(failed)} 件、読ませない場所 {len(deny)} か所）")
        out, cost, err, denials = run_improver(o, tp, prompt, deny, env)
        spend(cost)
        if cost is None:
            log("改善役の費用が分からないため、費用台帳に記録していません")
        report.update(cost_usd=cost, permission_denials=denials)
        if err:
            report["error"] = err
            log(err)
            _write_report(o.out, report)
            return 2, report
    finally:
        shutil.rmtree(root, ignore_errors=True)  # 依頼文・トランスクリプトの写しと認証の複製を残さない
    return write_candidate(o, out, report, failed, tasks, sorted(meta.get("tasks", {})), log)


def _clean_delta(d: Any, i: int, failed: dict[str, list[TrialRecord]]) -> tuple[dict[str, Any], int]:
    """(整えた変更項目, 失敗タスクにない予想の数)。予想は渡した失敗タスクの id だけを残す。"""
    d = d if isinstance(d, dict) else {}
    raw_fixes = d.get("fixes") if isinstance(d.get("fixes"), list) else []
    fixes = [str(x) for x in raw_fixes if str(x) in failed]
    clean = {"id": str(d.get("id") or f"d{i + 1}"), "kind": d.get("kind") if d.get("kind") in KINDS else "INV",
             **{k: str(d.get(k) or "") for k in ("file", "op", "old", "new", "reason")}, "fixes": fixes}
    return clean, len(raw_fixes) - len(fixes)


def write_candidate(o: ImproveOptions, out: dict[str, Any], report: dict[str, Any], failed: dict[str, list[TrialRecord]],
                    tasks: dict[str, Task], train_ids: list[str], log: Callable[[str], None]) -> tuple[int, dict[str, Any]]:
    """変更項目を現行の写しに適用し、事前検査を通れば dev・holdout・スキル比較のプロファイルを書き出す。"""
    deltas, dropped = [], 0
    for i, d in enumerate((out.get("deltas") if isinstance(out.get("deltas"), list) else [])[:MAX_DELTAS]):
        c, n = _clean_delta(d, i, failed)
        deltas.append(c)
        dropped += n
    harness = o.out / "harness"
    current = Path(tempfile.mkdtemp(prefix="harness-eval-current-"))
    try:
        materialize(o.profile, current)
        materialize(o.profile, harness)
        results = apply_deltas(harness, deltas)
        applied = [d for d, r in zip(deltas, results, strict=True) if r["applied"]]
        train = [tasks[t] for t in train_ids if t in tasks]
        problems = prechecks(current, harness, applied, [d for d, r in zip(deltas, results, strict=True) if not r["applied"]],
                             "\n".join(t.prompt for t in train), train_ids, grader_files(list(tasks.values())), o.max_growth)
        skills = changed_skills(current, harness)
    finally:
        shutil.rmtree(current, ignore_errors=True)
    predictions: dict[str, list[str]] = {}
    for d in applied:
        for t in d["fixes"]:
            predictions.setdefault(t, []).append(d["id"])
    report.update(notes=str(out.get("notes") or ""), deltas=[{**d, **r} for d, r in zip(deltas, results, strict=True)],
                  predictions=predictions, dropped_fixes=dropped, precheck={"passed": not problems, "problems": problems})
    if problems:
        _write_report(o.out, report)
        return 1, report
    desc = f"improve の候補（元: {o.profile.id}、実行 {report['run']['run_id']}）"
    # dev 用（現行と同じ id。gate は同じ id を前回と比べる）と holdout 用（別の id。promote は同じ実行の中で比べる）。
    # どちらも ../harness を指すので、配布物のハッシュは同じになる
    dev = write_profile(o.out / "dev" / f"{o.profile.id}.yaml", profile_raw(o.profile, o.profile.id, "../harness", desc))
    hold = write_profile(o.out / "holdout" / f"{o.candidate_id}.yaml", profile_raw(o.profile, o.candidate_id, "../harness", desc))
    report["profiles"] = {"dev": str(dev), "holdout": str(hold)}
    report["candidate"] = {"id": o.candidate_id, "harness_hash": load_profile(hold).harness_hash()}
    report["skills"] = {}
    for s in skills:
        try:
            report["skills"][s] = {k: str(v) for k, v in write_skill_variants(harness, s, o.profile, o.candidate_id, o.out).items()}
        except SpecError as e:
            report["skills"][s] = {"error": str(e)}
            log(f"スキル {s} の比較用プロファイルを作れません: {e}")
    _write_report(o.out, report)
    return 0, report


def _write_report(out: Path, report: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "improve.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
