"""ctrl-single-file-fix の採点: 単体テストが通り、clamp の境界の振る舞いが正しいか。

使い方: python check_single_file_fix.py（作業ディレクトリで実行。作業ディレクトリには書き込まない）
"""
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path.cwd()
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", ".", "-p", "test_clamp.py"], cwd=root,
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        print("単体テストが失敗: " + (r.stderr.strip().splitlines() or [""])[-1][:150])
        sys.exit(1)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    from clamp import clamp
    for args, want in (((10, 0, 10), 10), ((0, 0, 10), 0), ((11, 0, 10), 10), ((-1, 0, 10), 0), ((5.5, 0, 10), 5.5)):
        if clamp(*args) != want:
            print(f"clamp{args} が {clamp(*args)} です（期待 {want}）")
            sys.exit(1)
    print("一致")


main()
