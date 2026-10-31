"""改善の候補の組み立て: 配布物の展開・変更項目（差分）の適用・評価の前の事前検査・スキルの 3 つのプロファイル。

配布物は「設定ディレクトリ（{config_dir}）に入ったときの形」で扱う（CLAUDE.md・rules/・skills/<名前>/SKILL.md …）。
改善役が変えてよいのは、その中の Markdown の指示だけ（スクリプト・hooks・MCP などの実行される部分は人が書く）。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .harvest import NGRAM, _norm, leaked
from .isolation import snapshot
from .spec import Profile, SpecError

KINDS = ("INV", "DOM")  # 作業手順（invariant）／業務知識（domain）。Harness² の分け方
OPS = ("add", "replace", "remove")
ALWAYS_LOADED = ("CLAUDE.md", "AGENTS.md")  # これと rules/**/*.md は毎回の会話に読み込まれる
SKILL_VARIANTS = ("none", "description", "full")


def install_targets(profile: Profile) -> list[tuple[Path, str, bool]]:
    """install の (コピー元, {config_dir} からの相対パス, 末尾が / か)。編集できない形なら止める。"""
    out = []
    for i, step in enumerate(profile.install):
        if "copy" not in step:
            raise SpecError(f"{profile.path}: install[{i}] が run です。改善役は copy で入れる配布物だけを扱えます"
                            "（インストーラの中身は編集できないため。copy で入れる形のプロファイルを用意してください）")
        to = str(step["to"])
        if not to.startswith("{config_dir}"):
            raise SpecError(f"{profile.path}: install[{i}] のコピー先 {to} が {{config_dir}} の下ではありません（改善役が扱えるのは設定ディレクトリの配布物だけ）")
        src = Path(str(step["copy"]).replace("{profile_dir}", str(profile.base_dir)).replace("{python}", sys.executable))
        out.append((src if src.is_absolute() else profile.base_dir / src, to[len("{config_dir}"):].strip("/\\"), to.endswith(("/", "\\"))))
    return out


def materialize(profile: Profile, dest: Path) -> None:
    """プロファイルの配布物を、設定ディレクトリに入ったときの形で dest に書き出す。"""
    dest.mkdir(parents=True, exist_ok=True)
    for src, rel, to_dir in install_targets(profile):
        target = dest / rel if rel else dest
        if not src.exists():
            raise SpecError(f"{profile.path}: install のコピー元がありません: {src}")
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True)
            continue
        if not rel or to_dir or target.is_dir():
            target = target / src.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def target_problem(rel: str) -> str:
    """変更項目の対象ファイルとして使えなければ理由。Markdown の指示だけ・配布物の中だけ。
    OS によらず同じに判定する（区切りは / だけ。\\・先頭の /・ドライブ・..・. で始まる名前・制御文字は不可）。"""
    p = PurePosixPath(rel)
    if (not rel or rel.startswith("/") or "\\" in rel or ":" in rel or ".." in p.parts
            or any(part.startswith(".") for part in p.parts) or any(ord(c) < 32 for c in rel)):
        return f"配布物（harness/）の外を指しているか、使えない文字を含みます: {rel!r}"
    if p.suffix.lower() != ".md":
        return f"Markdown の指示ファイル以外は変えられません: {rel}（スクリプト・設定・hooks は人が書く）"
    return ""


def _read(f: Path) -> str:
    with open(f, encoding="utf-8", errors="replace", newline="") as fh:  # 改行（CRLF）をそのまま扱う。UTF-8 以外でも止まらない
        return fh.read()


def _write(f: Path, text: str) -> None:
    with open(f, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _apply_one(harness: Path, d: dict[str, Any]) -> str:
    rel, op, old, new = str(d.get("file", "")), d.get("op"), str(d.get("old") or ""), str(d.get("new") or "")
    why = target_problem(rel) or ("" if op in OPS else f"op が {list(OPS)} のどれでもありません: {op}")
    if why:
        return why
    f = harness / rel
    if not f.resolve().is_relative_to(harness.resolve()):  # 配布物の中のリンクから外へ書かない
        return f"配布物（harness/）の外を指しています: {rel}"
    text = _read(f) if f.is_file() else ""
    if "\r\n" in text:  # 改行が CRLF のファイルは、項目の改行もそろえる
        old, new = (t.replace("\r\n", "\n").replace("\n", "\r\n") for t in (old, new))
    nl = "\r\n" if "\r\n" in text else "\n"
    if op == "add":
        f.parent.mkdir(parents=True, exist_ok=True)
        _write(f, text + ("" if not text or text.endswith("\n") else nl) + new.rstrip("\r\n") + nl)
        return ""
    n = text.count(old) if old else 0
    if n != 1:
        return f"置き換える元の文が {n} 回見つかりました（ちょうど 1 回のときだけ適用）"
    _write(f, text.replace(old, new if op == "replace" else "", 1))
    return ""


def apply_deltas(harness: Path, deltas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """変更項目を順に適用する。戻り値は項目ごとの {id, applied, why}。
    add は末尾に追記（なければ作る）、replace / remove は old がちょうど 1 回出るときだけ。"""
    out = []
    for d in deltas:
        try:
            why = _apply_one(harness, d)
        except (OSError, ValueError) as e:  # 同名のファイルとフォルダの衝突など。項目ごとに「適用できない」として扱う
            why = f"書き込めません: {type(e).__name__}"
        out.append({"id": d.get("id"), "applied": not why, "why": why})
    return out


_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)  # コードの中の @ は読み込みではない
_IMPORT = re.compile(r"(?<![\w.@])@([^\s`@、。，．・：；！？「」『』（）]+)")  # CLAUDE.md の @path の読み込み（メールの @ は除く）


def _import_paths(text: str) -> list[str]:
    return [m.rstrip(".,;:!?)]}'\"") for m in _IMPORT.findall(_CODE.sub(" ", text))]


def _imports(f: Path, harness: Path, seen: set[Path], depth: int = 0) -> int:
    """f から @ で読み込まれる配布物の中のファイルの文字数（読み込みの先も 5 段までたどる）。"""
    total = 0
    for m in _import_paths(_read(f)) if depth < 5 else []:
        t = (f.parent / m).resolve()
        if t.is_file() and t.is_relative_to(harness.resolve()) and t not in seen:
            seen.add(t)
            total += len(_read(t)) + _imports(t, harness, seen, depth + 1)
    return total


def outside_imports(harness: Path) -> set[str]:
    """常時読み込まれる指示の @ のうち、はっきり配布物の外を指すもの（~ や / で始まる・.. を含む・外の実在のファイル）。
    @dataclass・@types/node のような、ファイルとして解決しない語は読み込みではないとみなす。"""
    out = set()
    for f in _always_files(harness):
        for m in _import_paths(_read(f)):
            t = (f.parent / m).resolve()
            if m.startswith(("~", "/")) or ".." in PurePosixPath(m).parts or (t.exists() and not t.is_relative_to(harness.resolve())):
                out.add(m)
    return out


def _always_files(harness: Path) -> list[Path]:
    files = [harness / n for n in ALWAYS_LOADED] + sorted((harness / "rules").rglob("*.md"))
    return [f for f in files if f.is_file()]


def always_loaded_chars(harness: Path) -> int:
    """毎回読み込まれる指示の文字数: CLAUDE.md・AGENTS.md・rules/**/*.md と、そこから @ で読み込まれるファイル、
    サブエージェント（agents/*.md）の説明（先頭の --- の部分）。スキルの説明文は数えない（3 つのプロファイルで比べる）。"""
    files = _always_files(harness)
    seen = {f.resolve() for f in files}
    total = sum(len(_read(f)) + _imports(f, harness, seen) for f in files)
    return total + sum(len(frontmatter(_read(f))) for f in sorted((harness / "agents").rglob("*.md")))


def _all_text(harness: Path) -> str:
    return "\n".join(_read(f) for f in sorted(harness.rglob("*.md")) if f.is_file())


def _spans(train: str, text: str) -> set[str]:
    """train の文のうち、text に NGRAM 文字以上そのまま入っている部分。"""
    t, body = _norm(train), _norm(text)
    return {t[i:i + NGRAM] for i in range(max(len(t) - NGRAM + 1, 0)) if t[i:i + NGRAM].strip() and t[i:i + NGRAM] in body}


def grader_terms(grader_names: set[str]) -> set[str]:
    """採点スクリプトの名前と、拡張子を除いた名前（6 文字以上のときだけ。短い名前は普通の語と重なる）。"""
    return {n.lower() for n in grader_names} | {Path(n).stem.lower() for n in grader_names if len(Path(n).stem) >= 6}


def _mentions(term: str, text: str) -> bool:
    """語として含む（validate.py の validate が invalidate や validated に当たらないよう、英数字の続きは別の語）。"""
    return re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", text) is not None


def prechecks(current: Path, candidate: Path, applied: list[dict[str, Any]], not_applied: list[dict[str, Any]],
              train_text: str, task_ids: list[str], grader_names: set[str], max_growth: int) -> list[str]:
    """評価の前に候補を落とす理由（空なら通過）。値そのもの（train の依頼の一部）は表示しないので、件数と項目 id だけを返す。
    1. train のタスク固有の文字列を足す（候補の全文と現行の全文の差で見る。項目をまたいで組み立てても見つける）
    2. 採点に関わるファイルを変える・採点スクリプトの名前を書く・Markdown 以外や配布物の外を変える
    3. 常時読み込まれる指示（@ で読み込まれる先・サブエージェントの説明を含む）を max_growth 文字より増やす"""
    problems = []
    if not applied:
        problems.append("適用できた変更項目がありません")
    cur_text, cand_text = _all_text(current), _all_text(candidate)
    terms = grader_terms(grader_names)
    for d in applied + not_applied:
        did, rel = d.get("id"), str(d.get("file", ""))
        if target_problem(rel):
            problems.append(f"{did}: {target_problem(rel)}")
        if PurePosixPath(rel).name.lower() in terms or PurePosixPath(rel).stem.lower() in terms:
            problems.append(f"{did}: 採点に使うファイルと同じ名前のファイルを変えます（{PurePosixPath(rel).name}）")
    named = sorted(g for g in terms if _mentions(g, cand_text.lower()) and not _mentions(g, cur_text.lower()))
    if named:
        problems.append(f"採点スクリプトの名前を書いています（{named}）。採点の中身に合わせた変更は holdout で通用しません")
    if train_text:
        tokens = {t for t in leaked(train_text, {"x": cand_text}) - leaked(train_text, {"x": cur_text}) if t != "<ngram>"}
        copies = _spans(train_text, cand_text) - _spans(train_text, cur_text)
        ids = sorted(t for t in task_ids if t.lower() in cand_text.lower() and t.lower() not in cur_text.lower())
        if tokens or copies or ids:
            who = sorted({str(d.get("id")) for d in applied
                          if any(x in _norm(str(d.get("new") or "")).lower() for x in tokens | {c.lower() for c in copies} | set(ids))})
            problems.append(f"train のタスク固有の文字列を足しています（依頼文の識別子・数値 {len(tokens)} 件・{NGRAM} 文字以上の一致 "
                            f"{len(copies)} か所{'・タスク id ' + str(ids) if ids else ''}。項目 {who or '（複数の項目の組み合わせ）'}）。"
                            "特定のタスクだけに効く変更は holdout で通用しません")
    added_outside = outside_imports(candidate) - outside_imports(current)
    if added_outside:
        problems.append(f"配布物の外のファイルを @ で読み込む指示を足しています（{len(added_outside)} 件）")
    growth = always_loaded_chars(candidate) - always_loaded_chars(current)
    if growth > max_growth:
        problems.append(f"常時読み込まれる指示（CLAUDE.md・AGENTS.md・rules/・その @ 読み込み・agents/ の説明）が {growth} 文字増えます"
                        f"（上限 {max_growth}）。置き換えで短くするか、必要なときだけ読まれるスキルに移してください")
    return problems


def changed_skills(current: Path, candidate: Path) -> list[str]:
    """候補で追加・変更されたスキルの名前。"""
    names = sorted(p.name for p in (candidate / "skills").iterdir() if p.is_dir()) if (candidate / "skills").is_dir() else []
    return [n for n in names if snapshot(candidate / "skills" / n) != snapshot(current / "skills" / n)]


def _fill_profile_dir(v: Any, base_dir: str) -> Any:
    if isinstance(v, str):
        return v.replace("{profile_dir}", base_dir)
    if isinstance(v, list):
        return [_fill_profile_dir(x, base_dir) for x in v]
    if isinstance(v, dict):
        return {k: _fill_profile_dir(x, base_dir) for k, x in v.items()}
    return v


def profile_raw(base: Profile, new_id: str, copy_src: str, description: str) -> dict[str, Any]:
    """base の設定（モデル・認証・env など）を引き継ぎ、配布物だけを copy_src（このプロファイルからの相対パス）に替えた定義。
    {profile_dir} は base のフォルダの絶対パスに置き換える（新しいプロファイルは別のフォルダに書くため）。
    hash_paths は外す（install の run の配布元を変更検知するためのもので、配布物は copy_src に丸ごと入っている）。"""
    raw = {k: _fill_profile_dir(v, str(base.base_dir.resolve())) for k, v in base.raw.items() if k != "hash_paths"}
    raw.update(id=new_id, description=description, install=[{"copy": copy_src, "to": "{config_dir}"}])
    return raw


def write_profile(path: Path, raw: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def frontmatter(text: str) -> str:
    """SKILL.md の先頭の --- で囲まれた部分（名前と説明文）。"""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return ""
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "".join(lines[:i + 1]).rstrip("\n") + "\n"
    return ""


def write_skill_variants(harness: Path, skill: str, base: Profile, prefix: str, out: Path) -> dict[str, Path]:
    """スキル 1 つについて「なし／説明文だけ／説明文＋本文」の 3 つの配布物とプロファイルを書き出す（それ以外は harness と同じ）。
    説明文だけでも、呼ばれ方が変わって振る舞いが変わる（The Regression Tax）ので、本文の効果と分けて測る。"""
    src = harness / "skills" / skill
    fm = frontmatter((src / "SKILL.md").read_text(encoding="utf-8")) if (src / "SKILL.md").is_file() else ""
    if not fm:
        raise SpecError(f"{src / 'SKILL.md'}: 先頭に --- で囲んだ name・description がありません")
    paths = {}
    for v in SKILL_VARIANTS:
        tree = out / skill / v
        if tree.exists():
            shutil.rmtree(tree)
        shutil.copytree(harness, tree)
        if v != "full":
            shutil.rmtree(tree / "skills" / skill)
        if v == "description":
            (tree / "skills" / skill).mkdir(parents=True)
            (tree / "skills" / skill / "SKILL.md").write_text(fm, encoding="utf-8")
        label = {"none": "なし", "description": "説明文だけ", "full": "説明文＋本文"}[v]
        paths[v] = write_profile(out / skill / f"{v}.yaml",
                                 profile_raw(base, f"{prefix}-{skill}-{v}", v, f"スキル {skill} の比較: {label}"))
    return paths
