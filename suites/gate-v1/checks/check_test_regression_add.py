"""gate-test-regression-add の採点（メタ検査）: 追加した再現テストが修正前のコードで落ち、修正後は全テストが通るか。

使い方: python check_test_regression_add.py（作業ディレクトリで実行。作業ディレクトリは書き換えず、一時フォルダへ複製して動かす）
  1. 修正後（作業ディレクトリのそのまま）: 全テストが通り、元のテスト 4 件が残っている
  2. 修正前のコード（下の BUGGY）に差し替えた複製: 追加したテストのうち 1 つ以上が落ち、落ちるのは追加分だけ
  3. 修正後のコードが依頼の不具合（max_len で切った末尾のハイフン）を直している
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ORIGINAL_IDS = {
    "tests.test_slugify.TestSlugify.test_basic",
    "tests.test_slugify.TestSlugify.test_punctuation",
    "tests.test_slugify.TestSlugify.test_empty",
    "tests.test_slugify.TestSlugify.test_max_len_on_word_boundary",
}

BUGGY = '''import re


def slugify(text, max_len=None):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if max_len is not None:
        slug = slug[:max_len]
    return slug
'''

RUNNER = r"""
import json, os, unittest

def flat(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            yield from flat(t)
        else:
            yield t

suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
ids = [t.id() for t in flat(suite)]
with open(os.devnull, "w") as null:
    res = unittest.TextTestRunner(stream=null, verbosity=0).run(suite)
bad = sorted({t.id() for t, _ in res.failures + res.errors})
print(json.dumps({"ids": ids, "failed": bad}))
"""


def fail(msg: str) -> None:
    print(msg)
    sys.exit(1)


def run_all(work: Path) -> dict:
    r = subprocess.run([sys.executable, "-B", "-c", RUNNER], cwd=work, capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        fail("テストを実行できません: " + (r.stderr.strip().splitlines() or [""])[-1][:150])
    return {}


def check(base: Path) -> None:
    fixed, buggy = base / "fixed", base / "buggy"
    shutil.copytree(Path.cwd(), fixed, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(fixed, buggy)
    (buggy / "slugs" / "core.py").write_text(BUGGY, encoding="utf-8")

    after = run_all(fixed)
    if after["failed"]:
        fail(f"修正後のテストが落ちます: {after['failed'][:2]}")
    missing = ORIGINAL_IDS - set(after["ids"])
    if missing:
        fail(f"元のテストが消えています: {sorted(missing)[:2]}")
    added = set(after["ids"]) - ORIGINAL_IDS
    if not added:
        fail("再現テストが追加されていません")

    before = run_all(buggy)
    failed_before = set(before["failed"])
    if not failed_before & added:
        fail("追加したテストが修正前のコードでも通ります（不具合を再現していません）")
    if failed_before & ORIGINAL_IDS:
        fail("元のテストが修正前のコードで落ちています（テストが書き換えられています）")

    sys.path.insert(0, str(fixed))
    sys.dont_write_bytecode = True
    from slugs import slugify
    cases = [(("Hello, World!", 6), "hello"), (("a-b-c-d", 3), "a-b"), (("Hello World", 5), "hello"), (("Hello World",), "hello-world")]
    for args, want in cases:
        if slugify(*args) != want:
            fail(f"slugify{args} が {slugify(*args)!r} です（期待 {want!r}）")
    print("一致")


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="gate-regress-"))
    try:
        check(base)
    finally:
        sys.path[:] = [p for p in sys.path if not p.startswith(str(base))]
        shutil.rmtree(base, ignore_errors=True)


main()
