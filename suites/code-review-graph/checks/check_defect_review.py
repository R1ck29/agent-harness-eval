"""crg-dXX の採点: 最終応答のレビューが、仕込んだ欠陥 1 件を 1 つの FINDING の中で指摘しているか。

使い方: python check_defect_review.py --defect D05（または環境変数 DEFECT_ID）
  - 環境変数 TRIAL_RECORD の記録 JSON の final_message を読む。空なら作業ディレクトリ
    （TRIAL_WORKDIR、なければカレント）の review.md を読む
  - 判定は同じフォルダの scoring.py（移植元 blind_review.py の MATCH / findings / scored と同じ規則）
終了コード: 0 = 一致 / 1 = 不一致・入力なし / 2 = 欠陥 ID の指定が不正
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scoring import MATCH, matched_finding  # noqa: E402


def review_text() -> str:
    path = os.environ.get("TRIAL_RECORD")
    text = ""
    if path and Path(path).is_file():
        text = json.loads(Path(path).read_text(encoding="utf-8")).get("final_message") or ""
    if not text.strip():
        review = Path(os.environ.get("TRIAL_WORKDIR") or ".") / "review.md"
        if review.is_file():
            text = review.read_text(encoding="utf-8", errors="replace")
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--defect", default=os.environ.get("DEFECT_ID"), help="欠陥 ID（例: D05）")
    defect = (ap.parse_args(argv).defect or "").strip().upper()
    if defect not in MATCH:
        print(f"欠陥 ID が不正です: {defect or '(なし)'}")
        return 2
    text = review_text()
    if not text.strip():
        print("レビュー本文がありません（final_message も review.md も空）")
        return 1
    if matched_finding(defect, text) is not None:
        print("一致")
        return 0
    print(f"{defect} を 1 つの FINDING の中で指摘した行がありません（必要な語と、ファイル跨ぎなら相手側の名指しが同じ行に要る）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
