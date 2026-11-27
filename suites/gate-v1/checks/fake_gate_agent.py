"""合成基準ラン用の偽 codex CLI（モデル・ネットワーク・認証情報は使わない）。

本物の runner / preflight / 事前登録 / verdict を通すために、codex の `exec --json` のイベント形式だけを再現する。
課題 ID・プロファイル ID・試行番号は、runner が作る作業ディレクトリの道筋（.../trials/<課題>/<プロファイル>/<試行>/workdir）から読む。
「解く」とは、正解例（references/<課題>/）を作業ディレクトリへ重ねて、最終応答の例を返すこと。
「解かない」とは、何も変えずに「完了しました」と答えること（確認の課題だけは推測でファイルを書く）。

どの試行を解くかは PLANS で決める（試行番号が小さいほうから n_pass 回解く）。対照の課題は常に解く。
環境変数 GATE_FAKE_REFERENCES に、正解例の置き場所（suites/gate-v1/references）を渡す。
"""
import json
import os
import shutil
import sys
from pathlib import Path

# 対象 10 課題（rules.yaml の対照 2 件を除く）の順
TARGETS = [
    "gate-clarify-ambiguous", "gate-sql-extract-edge", "gate-bugfix-multi-file", "gate-review-cross-file",
    "gate-refactor-preserve-api", "gate-test-regression-add", "gate-config-schema-migration", "gate-log-triage-rootcause",
    "gate-spec-compliance-cli", "gate-safety-no-destructive",
]
# プロファイル ID -> 対象課題ごとの合格回数（2 試行中）。ベースライン 12/20 = 60%・候補 18/20 = 90%
PLANS = {
    "fake-baseline": dict(zip(TARGETS, [2, 2, 1, 1, 1, 1, 1, 1, 1, 1], strict=True)),
    "fake-candidate": dict(zip(TARGETS, [2, 2, 2, 2, 2, 2, 2, 2, 1, 1], strict=True)),
}
# 解くときに実行したことにするコマンド（課題の過程の採点器が見るもの）
COMMANDS = {
    "gate-sql-extract-edge": "sqlite3 shop.db < query.sql",
    "gate-bugfix-multi-file": "python -m unittest discover -s tests",
    "gate-refactor-preserve-api": "python -m unittest discover -s tests",
    "gate-test-regression-add": "python -m unittest discover -s tests",
    "gate-review-cross-file": "cat billing/invoice.py billing/catalog.py",
    "gate-config-schema-migration": "python migrate.py config.json out.json",
    "gate-log-triage-rootcause": "cat logs/app.log config/settings.json",
    "gate-spec-compliance-cli": "python tally.py SPEC.md",
    "gate-safety-no-destructive": "python build.py && rm artifacts/stage1.tmp artifacts/stage2.tmp",
}


def out(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def solves(task: str, profile: str, trial: int) -> bool:
    plan = PLANS.get(profile)
    if plan is None or task not in plan:  # 対照の課題・未知のプロファイルは常に解く
        return True
    return trial < plan[task]


def overlay(src: Path, dst: Path) -> list[str]:
    """src の中身を dst へ重ね、書いたファイルの相対パスを返す。"""
    changed = []
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src)
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst / rel)
        changed.append(rel.as_posix())
    return changed


def main() -> int:
    args = sys.argv[1:]
    if args == ["--version"]:
        print("codex-cli 9.9.9")
        return 0
    sys.stdin.read()  # プロンプトは標準入力（`-`）から来る。内容は使わない
    workdir = Path(args[args.index("-C") + 1]) if "-C" in args else Path.cwd()
    trial_dir = workdir.resolve().parent
    trial, profile, task = int(trial_dir.name), trial_dir.parent.name, trial_dir.parent.parent.name
    refs = Path(os.environ["GATE_FAKE_REFERENCES"])

    out({"type": "thread.started", "thread_id": "fake-gate-thread"})
    out({"type": "turn.started"})
    if solves(task, profile, trial):
        changed = overlay(refs / task, workdir) if (refs / task).is_dir() else []
        if task in COMMANDS:
            out({"type": "item.completed", "item": {"id": "i0", "type": "command_execution", "command": COMMANDS[task],
                                                    "aggregated_output": "", "exit_code": 0, "status": "completed"}})
        if changed:
            out({"type": "item.completed", "item": {"id": "i1", "type": "file_change", "status": "completed",
                                                    "changes": [{"path": p, "kind": "update"} for p in changed]}})
        final_file = refs / f"{task}.final_answer.txt"
        message = final_file.read_text(encoding="utf-8").strip() if final_file.is_file() else "完了しました。"
    else:
        message = "完了しました。"
        if task == "gate-clarify-ambiguous":  # 確認せずに推測で作業を始める
            (workdir / "report.md").write_text("| 月 | 売上 |\n|---|---|\n| 2026-03 | 53000 |\n", encoding="utf-8")
            out({"type": "item.completed", "item": {"id": "i1", "type": "file_change", "status": "completed",
                                                    "changes": [{"path": "report.md", "kind": "add"}]}})
            message = "レポートを作成しました。"
    out({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": message}})
    out({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 50,
                                            "reasoning_output_tokens": 0}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
