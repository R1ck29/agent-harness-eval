"""タスク定義とプロファイル定義（YAML）の読み込みと検証。

未知のキーはエラーにする（typo による「設定したつもり」を防ぐため）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

AGENTS = ("claude_code", "codex")

GRADER_FIELDS: dict[str, set[str]] = {
    # 結果（outcome）を見るグレーダー
    "file_exists": {"path"},
    "file_contains": {"path", "pattern"},
    "regex": {"pattern"},
    "command": {"run", "timeout_s"},
    "llm": {"criteria", "focus", "max_chars"},
    # 過程（process）を見るグレーダー
    "tool_used": {"tool", "input_match", "min", "max", "include_subagents"},
    "skill_used": {"skill", "min", "max"},
    "subagent_used": {"name", "min", "max"},
    "hook_used": {"hook", "outcome", "min", "max"},
}
HOOK_OUTCOMES = {"success", "error"}  # Claude Code 2.1.306 の hook_response で確認できた値
GRADER_COMMON = {"type", "weight", "negate", "category", "description"}
PROCESS_GRADERS = {"tool_used", "skill_used", "subagent_used", "hook_used"}
REQUIRED: dict[str, set[str]] = {
    "file_exists": {"path"},
    "file_contains": {"path", "pattern"},
    "regex": {"pattern"},
    "command": {"run"},
    "llm": {"criteria"},
    "tool_used": {"tool"},
    "skill_used": {"skill"},
    "subagent_used": set(),
    "hook_used": {"hook"},
}
DEFAULT_CATEGORY = {
    "file_exists": "wrong_output",
    "file_contains": "wrong_output",
    "regex": "wrong_output",
    "command": "wrong_output",
    "llm": "wrong_output",
    "tool_used": "process_violation",
    "skill_used": "skill_selection",
    "subagent_used": "delegation",
    "hook_used": "hook",
}

TASK_KEYS = {"id", "description", "targets", "tags", "fixture", "prompt", "limits",
             "allow_tools", "deny_tools", "graders", "paired_negative"}
LIMIT_KEYS = {"max_turns", "timeout_s", "max_budget_usd"}
PROFILE_KEYS = {"id", "description", "agent", "model", "executable", "isolate_home",
                "install", "env", "env_unset", "extra_args", "permission_mode",
                "codex_sandbox", "pricing", "hash_paths", "copy_credentials",
                "isolation_settings", "billing"}
BILLINGS = ("api", "subscription")  # api: 呼び出しごとに課金（Bedrock・API キー）／ subscription: 定額（呼び出しごとの請求なし）
PRICING_KEYS = {"input_per_mtok", "cached_input_per_mtok", "output_per_mtok"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class SpecError(ValueError):
    pass


@dataclass
class Grader:
    type: str
    params: dict[str, Any]
    weight: float = 1.0
    negate: bool = False
    category: str = ""
    description: str = ""

    @property
    def kind(self) -> str:
        return "process" if self.type in PROCESS_GRADERS else "outcome"

    @property
    def requires_harness(self) -> bool:
        """配布物がないと通りようがない判定（スキル・サブエージェント・hook を 1 回以上使うことを要求）。
        ハーネスなしの基準と比べる Δ では両側とも外す（claude plugin eval と同じ扱い）。"""
        if self.type not in ("skill_used", "subagent_used", "hook_used") or self.negate:
            return False
        return int(self.params.get("min", 1 if "max" not in self.params else 0)) >= 1

    def label(self) -> str:
        if self.description:
            return self.description
        main = {k: v for k, v in self.params.items() if k in ("path", "pattern", "tool", "skill", "name", "hook", "outcome", "run", "criteria")}
        txt = ", ".join(f"{k}={v}" for k, v in main.items())
        return f"{'NOT ' if self.negate else ''}{self.type}({txt})"[:160]


@dataclass
class Task:
    id: str
    prompt: str
    path: Path
    description: str = ""
    targets: list[str] = field(default_factory=lambda: list(AGENTS))
    tags: list[str] = field(default_factory=list)
    fixture: Path | None = None
    max_turns: int = 20
    timeout_s: int = 600
    max_budget_usd: float | None = None
    allow_tools: list[str] = field(default_factory=list)
    deny_tools: list[str] = field(default_factory=list)
    graders: list[Grader] = field(default_factory=list)
    paired_negative: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def uses_hooks(self) -> bool:
        return any(g.type == "hook_used" for g in self.graders)

    def content_hash(self) -> str:
        """タスク定義 + フィクスチャ + 採点スクリプトの中身から計算するハッシュ（キャッシュ判定用。1 実行内では 1 回だけ計算）。
        結果に影響しない説明・タグ・paired_negative は含めない（書き換えても再実行しない）。"""
        if "_hash" in self.__dict__:
            return self.__dict__["_hash"]
        raw = {k: v for k, v in self.raw.items() if k not in ("description", "tags", "paired_negative")}
        h = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode())
        if self.fixture:
            _hash_tree(h, self.fixture)
        for g in self.graders:  # command グレーダーが参照するスクリプトも含める
            if g.type == "command":
                run = str(g.params.get("run", "")).replace("{task_dir}", ".")
                for tok in re.findall(r"[\w./\\-]+\.(?:py|sh|ps1|js)", run):
                    p = (self.path.parent / tok)
                    if p.is_file():
                        h.update(p.read_bytes())
        self.__dict__["_hash"] = h.hexdigest()[:16]
        return self.__dict__["_hash"]


@dataclass
class Profile:
    id: str
    agent: str
    path: Path
    description: str = ""
    model: str | None = None
    executable: list[str] | None = None
    isolate_home: bool = True
    install: list[dict[str, Any]] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_unset: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    permission_mode: str | None = None
    codex_sandbox: str = "workspace-write"
    pricing: dict[str, float] | None = None
    hash_paths: list[str] = field(default_factory=list)
    copy_credentials: bool = False
    billing: str = "api"
    isolation_settings: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def content_hash(self) -> str:
        """プロファイル定義 + コピー元ファイル + hash_paths の中身から計算するハッシュ（1 実行内では 1 回だけ計算）。"""
        if "_hash" in self.__dict__:
            return self.__dict__["_hash"]
        h = hashlib.sha256(json.dumps(self.raw, sort_keys=True, ensure_ascii=False).encode())
        srcs = [step["copy"] for step in self.install if "copy" in step] + list(self.hash_paths)
        for s in srcs:
            # install と同じ置換をしてから探す（{profile_dir} / {python} を使ったコピー元も変更検知する）
            s = str(s).replace("{profile_dir}", str(self.base_dir)).replace("{python}", sys.executable)
            p = Path(s) if Path(s).is_absolute() else self.base_dir / s
            if p.exists():
                _hash_tree(h, p)
            else:
                h.update(f"missing:{s}".encode())
        self.__dict__["_hash"] = h.hexdigest()[:16]
        return self.__dict__["_hash"]


def _hash_tree(h: Any, p: Path) -> None:
    if p.is_file():
        h.update(p.name.encode())
        h.update(p.read_bytes())
        return
    for f in sorted(x for x in p.rglob("*") if x.is_file()):
        h.update(f.relative_to(p).as_posix().encode())
        h.update(f.read_bytes())


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SpecError(f"{path}: YAML の構文エラー: {e}") from e
    if not isinstance(data, dict):
        raise SpecError(f"{path}: トップレベルはマッピングである必要があります")
    return data


def _check_keys(path: Path, where: str, data: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise SpecError(f"{path}: {where} に未知のキー {sorted(unknown)}（使えるキー: {sorted(allowed)}）")


def _str_list(path: Path, name: str, v: Any) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise SpecError(f"{path}: {name} は文字列のリストである必要があります")
    return v


def parse_grader(path: Path, i: int, g: Any) -> Grader:
    if not isinstance(g, dict) or "type" not in g:
        raise SpecError(f"{path}: graders[{i}] に type がありません")
    t = g["type"]
    if t not in GRADER_FIELDS:
        raise SpecError(f"{path}: graders[{i}] の type '{t}' は未対応（{sorted(GRADER_FIELDS)}）")
    _check_keys(path, f"graders[{i}]({t})", g, GRADER_FIELDS[t] | GRADER_COMMON)
    missing = REQUIRED[t] - set(g)
    if missing:
        raise SpecError(f"{path}: graders[{i}]({t}) に必須キー {sorted(missing)} がありません")
    for key in ("pattern", "input_match", "tool", "hook"):
        if key in g:
            try:
                re.compile(str(g[key]))
            except re.error as e:
                raise SpecError(f"{path}: graders[{i}] の {key} が正規表現として不正: {e}") from e
    for key in ("min", "max"):
        if key in g and (not isinstance(g[key], int) or g[key] < 0):
            raise SpecError(f"{path}: graders[{i}] の {key} は 0 以上の整数")
    if isinstance(g.get("min"), int) and isinstance(g.get("max"), int) and g["min"] > g["max"]:
        raise SpecError(f"{path}: graders[{i}] の min が max より大きい")
    if "outcome" in g and g["outcome"] not in HOOK_OUTCOMES:
        raise SpecError(f"{path}: graders[{i}] の outcome は {sorted(HOOK_OUTCOMES)} のどれか（実測で確認できた値）")
    w = g.get("weight", 1)
    if not isinstance(w, (int, float)) or w <= 0:
        raise SpecError(f"{path}: graders[{i}] の weight は正の数")
    params = {k: v for k, v in g.items() if k not in GRADER_COMMON}
    return Grader(type=t, params=params, weight=float(w), negate=bool(g.get("negate", False)),
                  category=str(g.get("category") or DEFAULT_CATEGORY[t]),
                  description=str(g.get("description", "")))


def load_task(path: Path) -> Task:
    d = _load_yaml(path)
    _check_keys(path, "タスク", d, TASK_KEYS)
    for req in ("id", "prompt", "graders"):
        if req not in d:
            raise SpecError(f"{path}: 必須キー '{req}' がありません")
    if not ID_RE.match(str(d["id"])):
        raise SpecError(f"{path}: id '{d['id']}' は英数字・_ . - のみ")
    targets = _str_list(path, "targets", d.get("targets", list(AGENTS)))
    for t in targets:
        if t not in AGENTS:
            raise SpecError(f"{path}: targets の '{t}' は未対応（{AGENTS}）")
    limits = d.get("limits") or {}
    _check_keys(path, "limits", limits, LIMIT_KEYS)
    fixture = None
    if d.get("fixture"):
        fixture = (path.parent / d["fixture"]).resolve()
        if not fixture.exists():
            raise SpecError(f"{path}: fixture が見つかりません: {fixture}")
    graders = d.get("graders") or []
    if not isinstance(graders, list) or not graders:
        raise SpecError(f"{path}: graders は 1 つ以上必要です")
    return Task(
        id=str(d["id"]), prompt=str(d["prompt"]), path=path.resolve(),
        description=str(d.get("description", "")), targets=targets,
        tags=_str_list(path, "tags", d.get("tags")), fixture=fixture,
        max_turns=int(limits.get("max_turns", 20)), timeout_s=int(limits.get("timeout_s", 600)),
        max_budget_usd=limits.get("max_budget_usd"),
        allow_tools=_str_list(path, "allow_tools", d.get("allow_tools")),
        deny_tools=_str_list(path, "deny_tools", d.get("deny_tools")),
        graders=[parse_grader(path, i, g) for i, g in enumerate(graders)],
        paired_negative=d.get("paired_negative"), raw=d,
    )


def load_profile(path: Path) -> Profile:
    d = _load_yaml(path)
    _check_keys(path, "プロファイル", d, PROFILE_KEYS)
    for req in ("id", "agent"):
        if req not in d:
            raise SpecError(f"{path}: 必須キー '{req}' がありません")
    if not ID_RE.match(str(d["id"])):
        raise SpecError(f"{path}: id '{d['id']}' は英数字・_ . - のみ")
    if d["agent"] not in AGENTS:
        raise SpecError(f"{path}: agent '{d['agent']}' は未対応（{AGENTS}）")
    install = d.get("install") or []
    if not isinstance(install, list):
        raise SpecError(f"{path}: install はリストである必要があります")
    for i, step in enumerate(install):
        if not isinstance(step, dict) or not (("copy" in step and "to" in step and set(step) <= {"copy", "to"})
                                             or (set(step) == {"run"})):
            raise SpecError(f"{path}: install[{i}] は {{copy: <元>, to: <先>}} または {{run: <コマンド>}}")
    env = d.get("env") or {}
    if not isinstance(env, dict):
        raise SpecError(f"{path}: env はマッピング")
    pricing = d.get("pricing")
    if pricing is not None:
        if not isinstance(pricing, dict):
            raise SpecError(f"{path}: pricing はマッピング")
        _check_keys(path, "pricing", pricing, PRICING_KEYS)
    exe = d.get("executable")
    if exe is not None:
        exe = [exe] if isinstance(exe, str) else _str_list(path, "executable", exe)
    billing = d.get("billing", "api")
    if billing not in BILLINGS:
        raise SpecError(f"{path}: billing は {' / '.join(BILLINGS)}（月の予算に数えるのは api だけ）")
    sandbox = d.get("codex_sandbox", "workspace-write")
    if sandbox not in ("read-only", "workspace-write", "danger-full-access"):
        raise SpecError(f"{path}: codex_sandbox は read-only / workspace-write / danger-full-access")
    return Profile(
        id=str(d["id"]), agent=d["agent"], path=path.resolve(), description=str(d.get("description", "")),
        model=d.get("model"), executable=exe, isolate_home=bool(d.get("isolate_home", True)),
        install=install, env={str(k): str(v) for k, v in env.items()},
        env_unset=_str_list(path, "env_unset", d.get("env_unset")),
        extra_args=[str(x) for x in (d.get("extra_args") or [])],
        permission_mode=d.get("permission_mode"), codex_sandbox=sandbox, pricing=pricing,
        hash_paths=_str_list(path, "hash_paths", d.get("hash_paths")),
        copy_credentials=bool(d.get("copy_credentials", False)),
        isolation_settings=bool(d.get("isolation_settings", True)), billing=billing, raw=d,
    )


def _expand(paths: list[str], exts: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            out += sorted(x for x in pp.rglob("*") if x.suffix in exts and x.is_file())
        elif pp.is_file():
            out.append(pp)
        else:
            raise SpecError(f"見つかりません: {p}")
    return out


def load_tasks(paths: list[str]) -> list[Task]:
    tasks = [load_task(p) for p in _expand(paths, (".yaml", ".yml"))]
    ids = [t.id for t in tasks]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise SpecError(f"タスク id が重複: {sorted(dup)}")
    return tasks


def load_profiles(paths: list[str]) -> list[Profile]:
    profiles = [load_profile(p) for p in _expand(paths, (".yaml", ".yml"))]
    ids = [p.id for p in profiles]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise SpecError(f"プロファイル id が重複: {sorted(dup)}")
    return profiles


def lint(tasks: list[Task], profiles: list[Profile]) -> list[str]:
    """設計ルールに沿っているかの警告（エラーではない）。"""
    warns: list[str] = []
    ids = {t.id for t in tasks}
    negatives = {t.paired_negative for t in tasks if t.paired_negative}
    agents = {p.agent for p in profiles}
    for t in tasks:
        kinds = {g.kind for g in t.graders}
        if "outcome" not in kinds:
            warns.append(f"{t.id}: 結果を見るグレーダー（file_*/regex/command/llm）がありません")
        if "process" not in kinds:
            warns.append(f"{t.id}: 過程を見るグレーダー（tool_used/skill_used/subagent_used/hook_used）がありません")
        should_use = [g for g in t.graders if g.type in ("skill_used", "subagent_used") and not g.negate
                      and int(g.params.get("min", 1 if "max" not in g.params else 0)) >= 1]
        if should_use and not t.paired_negative and t.id not in negatives:
            warns.append(f"{t.id}: スキル／サブエージェントを「使うべき」タスクですが paired_negative（使うべきでないタスク）がありません")
        if t.paired_negative and t.paired_negative not in ids:
            warns.append(f"{t.id}: paired_negative '{t.paired_negative}' が読み込んだタスクにありません")
        if "codex" in t.targets and "codex" in agents and any(g.type == "skill_used" for g in t.graders):
            warns.append(f"{t.id}: Codex のスキル検出は推定（SKILL.md を読むコマンドの検出）です。成果物グレーダーを主にしてください")
        if "codex" in t.targets and "codex" in agents and any(g.type == "hook_used" for g in t.graders):
            warns.append(f"{t.id}: Codex は hooks の実行を記録しないため、Codex の試行では hook_used を判定しません（not_run）")
        if "claude_code" in t.targets and "claude_code" in agents and not t.allow_tools:
            warns.append(f"{t.id}: allow_tools が空です。-p モードでは承認が必要なツール（Write/Edit/Bash 等）は拒否されます")
    if not any(not p.install for p in profiles):
        warns.append("ハーネスなし（install が空）のベースラインプロファイルがありません。Δ の比較対象になります")
    return warns
