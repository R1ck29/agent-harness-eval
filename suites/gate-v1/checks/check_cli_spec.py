"""gate-spec-compliance-cli の採点: tally.py を SPEC.md どおりかブラックボックスで検証する。

使い方: python check_cli_spec.py（作業ディレクトリの tally.py を、一時フォルダ内の入力ファイルで実行する。作業ディレクトリには書き込まない）
確認すること: 出力形式と並び順・--top・stdin（-）・空ファイル・--help・終了コード 0/1/2・エラー時は stdout が空で stderr に error:/usage:
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TEXT = "The cat and the hat. THE end, and Cat!\nzebra apple apple"
# 期待: the 3 / and 2 / apple 2 / cat 2 / end 1 / hat 1 / zebra 1（回数の多い順、同数は辞書順）
FULL = "the 3\nand 2\napple 2\ncat 2\nend 1\nhat 1\nzebra 1\n"

# (名前, 引数（{f}=入力ファイル・{empty}=空ファイル・{missing}=存在しないファイル）, stdin, 期待終了コード, 期待 stdout（None は空）)
CASES = [
    ("full", ["{f}"], None, 0, FULL),
    ("top2", ["--top", "2", "{f}"], None, 0, "the 3\nand 2\n"),
    ("top_after_file", ["{f}", "--top", "1"], None, 0, "the 3\n"),
    ("top_larger_than_words", ["--top", "99", "{f}"], None, 0, FULL),
    ("stdin", ["-"], "b a b", 0, "b 2\na 1\n"),
    ("empty_file", ["{empty}"], None, 0, ""),
    ("no_words", ["-"], "123 !!! ---", 0, ""),
    ("help", ["--help"], None, 0, "usage"),
    ("missing_file", ["{missing}"], None, 1, None),
    ("no_args", [], None, 2, None),
    ("top_zero", ["--top", "0", "{f}"], None, 2, None),
    ("top_negative", ["--top", "-3", "{f}"], None, 2, None),
    ("top_not_int", ["--top", "x", "{f}"], None, 2, None),
    ("top_no_value", ["--top"], None, 2, None),
    ("unknown_option", ["--bogus", "{f}"], None, 2, None),
]


def fail(msg: str) -> None:
    print(msg)
    sys.exit(1)


def check(base: Path) -> None:
    work = base / "w"
    shutil.copytree(Path.cwd(), work, ignore=shutil.ignore_patterns("__pycache__"))
    if not (work / "tally.py").is_file():
        fail("tally.py がありません")
    files = {"f": base / "in.txt", "empty": base / "empty.txt", "missing": base / "nope.txt"}
    files["f"].write_text(TEXT, encoding="utf-8")
    files["empty"].write_text("", encoding="utf-8")
    for name, args, stdin, code, out in CASES:
        argv = [a.format(**{k: str(v) for k, v in files.items()}) if a.startswith("{") else a for a in args]
        r = subprocess.run([sys.executable, "-B", "tally.py", *argv], cwd=work, input=stdin, capture_output=True, text=True,
                           timeout=30)
        if r.returncode != code:
            fail(f"{name}: 終了コードが {r.returncode}（期待 {code}）")
        if code == 0 and name == "help":
            if "usage" not in r.stdout.lower() or r.stderr.strip():
                fail(f"{name}: usage を stdout に出し、stderr は空にしてください")
        elif code == 0:
            if r.stdout != out or r.stderr.strip():
                fail(f"{name}: stdout/stderr が違います: {r.stdout[:60]!r} / {r.stderr[:40]!r}")
        else:
            if r.stdout.strip() or not r.stderr.strip().lower().startswith(("error:", "usage:")):
                fail(f"{name}: エラー時は stdout が空で、stderr が error: か usage: で始まる必要があります")
    print("一致")


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="gate-cli-"))
    try:
        check(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


main()
