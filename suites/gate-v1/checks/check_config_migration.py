"""gate-config-schema-migration の採点: migrate.py を隠しケースでブラックボックスに検証する。

使い方: python check_config_migration.py（作業ディレクトリの migrate.py を、一時フォルダ内の入力ファイルで実行する）
確認すること: v1 -> v2 の変換結果・既定値・未知キーの拒否・不正入力の終了コード 2 と stderr の `error:`・
  入力ファイルが変わらない・失敗時に出力を作らない・v2 を再移行しても内容が変わらない（冪等）・2 回実行して同じ結果
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# (名前, 入力の内容（dict は JSON にする・str はそのまま書く）, 期待する出力 dict。None は終了コード 2 を期待)
CASES = [
    ("full", {"version": 1, "host": "localhost", "port": 9000, "debug": True, "timeout": 2.5},
     {"version": 2, "server": {"host": "localhost", "port": 9000}, "log_level": "debug", "timeout_ms": 2500}),
    ("defaults", {"version": 1, "host": "example.test"},
     {"version": 2, "server": {"host": "example.test", "port": 8080}, "log_level": "info", "timeout_ms": 30000}),
    ("debug_false_int_timeout", {"version": 1, "host": "h", "port": 1, "debug": False, "timeout": 3},
     {"version": 2, "server": {"host": "h", "port": 1}, "log_level": "info", "timeout_ms": 3000}),
    ("zero_timeout", {"version": 1, "host": "h", "timeout": 0},
     {"version": 2, "server": {"host": "h", "port": 8080}, "log_level": "info", "timeout_ms": 0}),
    ("v2_passthrough", {"version": 2, "server": {"host": "h", "port": 5}, "log_level": "debug", "timeout_ms": 700},
     {"version": 2, "server": {"host": "h", "port": 5}, "log_level": "debug", "timeout_ms": 700}),
    ("unknown_key_v1", {"version": 1, "host": "h", "colour": "red"}, None),
    ("unknown_key_v2", {"version": 2, "server": {"host": "h", "port": 5}, "log_level": "info", "timeout_ms": 1, "extra": 1}, None),
    ("unknown_key_nested", {"version": 2, "server": {"host": "h", "port": 5, "tls": True}, "log_level": "info", "timeout_ms": 1}, None),
    ("missing_host", {"version": 1, "port": 1}, None),
    ("bad_type_port", {"version": 1, "host": "h", "port": "80"}, None),
    ("bad_version", {"version": 3, "host": "h"}, None),
    ("no_version", {"host": "h"}, None),
    ("invalid_json", "{not json", None),
    ("not_an_object", "[1, 2]", None),
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(work: Path, src: Path, dst: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", "migrate.py", str(src), str(dst)], cwd=work, capture_output=True, text=True,
                          timeout=30, stdin=subprocess.DEVNULL)


def fail(msg: str) -> None:
    print(msg)
    sys.exit(1)


def check(base: Path) -> None:
    work = base / "w"
    shutil.copytree(Path.cwd(), work, ignore=shutil.ignore_patterns("__pycache__"))
    if not (work / "migrate.py").is_file():
        fail("migrate.py がありません")
    for name, content, want in CASES:
        src, dst = base / f"{name}.json", base / f"{name}.out.json"
        src.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        before = digest(src)
        r = run(work, src, dst)
        if digest(src) != before:
            fail(f"{name}: 入力ファイルが書き換えられました")
        if want is None:
            if r.returncode != 2 or not r.stderr.strip().startswith("error:") or r.stdout.strip() or dst.exists():
                fail(f"{name}: 不正入力は終了コード 2・stderr の error:・stdout なし・出力なし のはずです（rc={r.returncode}）")
            continue
        if r.returncode != 0 or not dst.is_file():
            fail(f"{name}: 成功するはずが rc={r.returncode}: {r.stderr.strip()[:100]}")
        text = dst.read_text(encoding="utf-8")
        try:
            got = json.loads(text)
        except ValueError:
            fail(f"{name}: 出力が JSON ではありません")
        if got != want or not text.endswith("\n") or "\n  " not in text:
            fail(f"{name}: 出力が違います: {text[:120]!r}")
        if not isinstance(got["timeout_ms"], int) or isinstance(got["timeout_ms"], bool):
            fail(f"{name}: timeout_ms が整数ではありません")
        again = base / f"{name}.again.json"  # 冪等性: 出力をもう一度移行しても同じ内容
        r2 = run(work, dst, again)
        if r2.returncode != 0 or again.read_text(encoding="utf-8") != text:
            fail(f"{name}: 移行済みの出力を再移行すると内容が変わります（冪等ではありません）")
        dst2 = base / f"{name}.out2.json"  # 同じ入力を 2 回実行しても同じ出力
        run(work, src, dst2)
        if not dst2.is_file() or dst2.read_text(encoding="utf-8") != text:
            fail(f"{name}: 同じ入力を 2 回実行すると出力が変わります")
    print("一致")


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="gate-migrate-"))
    try:
        check(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


main()
