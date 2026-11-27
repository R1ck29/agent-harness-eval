"""合成基準ラン用のフェイクプロファイル（fake-baseline / fake-candidate）を書き出す。

使い方: python make_fake_profiles.py <出力ディレクトリ>
どちらも agent: codex・billing: subscription で、実行ファイルは偽 CLI（fake_gate_agent.py）。モデル・ネットワーク・認証情報は使わない。
試行は bwrap の中で動く（Linux）。偽 CLI が正解例を読めるよう sandbox_ro で references を見せる。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent


def write_profile(out_dir: Path, pid: str, description: str) -> Path:
    path = out_dir / f"{pid}.yaml"
    refs = str(SUITE / "references")
    lines = [
        f"id: {pid}",
        f"description: {description}",
        "agent: codex",
        "billing: subscription  # 呼び出しごとの請求がない（偽 CLI）。費用の上限の見積もりに入れない",
        f"executable: [{json.dumps(sys.executable)}, {json.dumps(str(HERE / 'fake_gate_agent.py'))}]",
        "env:",
        f"  GATE_FAKE_REFERENCES: {json.dumps(refs)}",
        f"sandbox_ro: [{json.dumps(refs)}]",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    write_profile(out_dir, "fake-baseline", "合成基準ランのベースライン（対象課題の約 60% を解く）")
    write_profile(out_dir, "fake-candidate", "合成基準ランの候補（対象課題の約 90% を解く）")
    print(f"フェイクプロファイルを {out_dir} に書きました")


main()
