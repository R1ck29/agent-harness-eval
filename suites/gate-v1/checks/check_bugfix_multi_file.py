"""gate-bugfix-multi-file の採点: 既存テスト（無改変）と隠し回帰テストの両方が通るか。

使い方: python check_bugfix_multi_file.py（作業ディレクトリで実行。作業ディレクトリは書き換えず、一時フォルダへ複製して動かす）
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 見えているテストのハッシュ（改変されていないこと）。fixtures の tests/test_quote.py を LF にそろえた sha256（Windows の CRLF チェックアウトでも一致させる）
VISIBLE = {"tests/test_quote.py": "68a50ab130e09cb35e6abeb1232136fa5fb1d46bc9f575d6925bd5cf154c47de"}

HIDDEN = '''
import unittest

from shipping import ShippingError, quote
from shipping.errors import ShippingError as Err2
from shipping.rates import rate_for
from shipping.weight import billable_weight


class Hidden(unittest.TestCase):
    def test_error_is_exported_once(self):
        self.assertIs(ShippingError, Err2)

    def test_unknown_zone(self):
        for bad in ("Z", "", None, 3):
            with self.subTest(zone=bad):
                with self.assertRaises(ShippingError):
                    rate_for(bad)
                with self.assertRaises(ShippingError):
                    quote(bad, 1.0)

    def test_zone_case(self):
        self.assertEqual(rate_for("a"), 5.0)

    def test_rounds_up(self):
        for kg, want in ((0.1, 0.5), (1.0, 1.0), (1.2, 1.5), (2.01, 2.5), (2.5, 2.5)):
            with self.subTest(kg=kg):
                self.assertEqual(billable_weight(kg), want)

    def test_bad_weight(self):
        for bad in (0, -1, 0.0, None, "2", True):
            with self.subTest(kg=bad):
                with self.assertRaises(ShippingError):
                    billable_weight(bad)
                with self.assertRaises(ShippingError):
                    quote("A", bad)

    def test_quote_uses_rounded_up_weight(self):
        self.assertEqual(quote("B", 2.2), 20.0)
        self.assertEqual(quote("c", 0.1), 6.25)


if __name__ == "__main__":
    unittest.main()
'''


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", *args], cwd=cwd, capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)


def main() -> None:
    src = Path.cwd()
    for rel, digest in VISIBLE.items():
        f = src / rel
        if not f.is_file() or hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != digest:
            print(f"見えているテスト {rel} が変更されています")
            sys.exit(1)
    base = Path(tempfile.mkdtemp(prefix="gate-bugfix-"))
    try:
        work = base / "w"
        shutil.copytree(src, work, ignore=shutil.ignore_patterns("__pycache__"))
        (work / "_gate_hidden.py").write_text(HIDDEN, encoding="utf-8")
        for name, args in (("既存テスト", ("-m", "unittest", "discover", "-s", "tests", "-t", ".")),
                           ("隠し回帰テスト", ("-m", "unittest", "_gate_hidden"))):
            r = run(work, *args)
            if r.returncode != 0:
                tail = (r.stderr.strip().splitlines() or [""])[-1]
                print(f"{name}が失敗: {tail[:150]}")
                sys.exit(1)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("一致")


main()
