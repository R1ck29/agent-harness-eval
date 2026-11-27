"""gate-log-triage-rootcause の採点: triage.json が真の原因を指し、おとり（DNS の遅延）を原因にしていないか。

使い方: python check_log_triage.py（作業ディレクトリの triage.json を読む。作業ディレクトリには書き込まない）
真の原因: deploy v1.4.2 で pool_size が 8 -> 2 に下がり、worker_threads=8 のままなので接続待ちがタイムアウトする。
おとり: dns_cache_ttl_s が 0 になり DNS の警告が大量に出ているが、ジョブの失敗とは無関係。
"""
import json
import re
import sys
from pathlib import Path


def fail(msg: str) -> None:
    print(msg)
    sys.exit(1)


def main() -> None:
    path = Path("triage.json")
    if not path.is_file():
        fail("triage.json がありません")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        fail("triage.json が JSON ではありません")
    if not isinstance(data, dict):
        fail("triage.json は JSON オブジェクトにしてください")
    for key, typ in (("root_cause", str), ("evidence", list), ("files", list), ("fix", str)):
        if not isinstance(data.get(key), typ) or not data[key]:
            fail(f"必須キー {key} がない・空・型違いです")
    cause = data["root_cause"].lower()
    if not re.search(r"pool[_ ]?size", cause) or not re.search(r"worker[_ ]?threads|\b8\b|スレッド|ワーカ", cause):
        fail("root_cause が pool_size と worker_threads の不整合を指していません")
    if re.search(r"dns|名前解決|resolver|キャッシュ", cause):
        fail("root_cause がおとり（DNS）を原因に含めています")
    files = [str(f).replace("\\", "/") for f in data["files"]]
    if "config/settings.json" not in files:
        fail("files に config/settings.json（原因の設定）がありません")
    ev = [str(e) for e in data["evidence"]]
    if len(ev) < 2 or not any("app.log" in e for e in ev) or not any(re.search(r"deploy|settings|pool\.py", e) for e in ev):
        fail("evidence が 2 件以上で、ログ（app.log）と設定・デプロイの根拠の両方を含む必要があります")
    if not re.search(r"pool[_ ]?size|worker[_ ]?threads|スレッド|プール", data["fix"], re.IGNORECASE):
        fail("fix が pool_size / worker_threads の調整になっていません")
    print("一致")


main()
