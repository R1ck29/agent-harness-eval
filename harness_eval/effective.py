"""試行の有効設定: 信頼の付与・期待する部品・観測した部品・その差の記録（docs/design/REDESIGN.md §2・§6.2）。

なぜ必要か（Claude Code 2.1.325・codex-cli 0.169.0 で実測）:
- 未信頼のフォルダでは、プロジェクト単位の設定の一部が黙って捨てられる。Claude Code は `.claude/settings.json` の
  `permissions.allow` を（標準エラーに 1 行出すだけで）使わず、Codex は `.codex/config.toml` 全体（指示・MCP）を何も出さずに使わない。
  普段の利用者は自分のリポジトリを一度信頼しているので、隔離した設定の中で作業ディレクトリを信頼済みにしてから起動する
- 置いたつもりの部品が読み込まれないこと（置き場所・frontmatter の誤り、CLI の仕様変更）は、結果だけ見ると
  「ハーネスの効果がない」に見える。置いた部品（期待）と CLI が読み込んだ部品（観測）を照合して記録する

秘密は記録しない: 環境変数は名前だけ、認証ファイル（.credentials.json・auth.json）は一覧から外し、
起動コマンドの key=・token= などの値は伏せる。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

SCHEMA = 1
CREDENTIAL_FILES = {".credentials.json", "auth.json"}
KINDS = ("skills", "agents", "mcp_servers", "instructions")
MAX_INSTALLED = 500  # 記録するファイル数の上限（大きな配布物でもレコードを膨らませない）
_SECRET_ARG = re.compile(r"(?i)((?:key|token|secret|password)[\w.-]*\s*[=:]\s*)\S+")


# ---------------------------------------------------------------- 信頼の付与
def seed_trust(agent: str, config_dir: Path, workdir: Path) -> list[str]:
    """隔離した設定に「作業ディレクトリは信頼済み」と書く。信頼したパスを返す。既存の内容（install が置いた設定）とは統合する。

    - Claude Code: `{config_dir}/.claude.json` の projects[<パス>].hasTrustDialogAccepted = true。信頼は git のルート単位なので
      （作業ディレクトリが親のリポジトリの中にあると、警告がリポジトリのルートを名指しする。実測）、作業ディレクトリとすべての祖先に付ける。
      祖先の指示ファイルは ancestor_instructions で実行前に止めるので、祖先を信頼しても余計なものは読み込まれない
    - Codex: `{config_dir}/config.toml` に [projects."<workdir>"] trust_level = "trusted"（親のリポジトリの中でもこれで読み込まれた。実測）
    """
    wd = Path(workdir).resolve()
    if agent == "claude_code":
        f = Path(config_dir) / ".claude.json"
        data: Any = {}
        if f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8") or "{}")
            except (ValueError, UnicodeDecodeError) as e:
                raise RuntimeError(f"{f} が JSON として読めません（信頼を付けられません）: {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"{f} のトップレベルがオブジェクトではありません")
        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = data["projects"] = {}
        paths = []
        for p in (wd, *wd.parents):
            for key in dict.fromkeys((p.as_posix(), str(p))):  # Windows では両方の書式（未実測。REDESIGN.md §10 フェーズ 4）
                entry = projects.get(key)
                projects[key] = {**(entry if isinstance(entry, dict) else {}), "hasTrustDialogAccepted": True}
                paths.append(key)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return list(dict.fromkeys(paths))
    if agent == "codex":
        f = Path(config_dir) / "config.toml"
        text = f.read_text(encoding="utf-8") if f.is_file() else ""
        header = f"[projects.{json.dumps(str(wd))}]"  # JSON の文字列は TOML の基本文字列としても正しい（\ と " をエスケープ）
        if header not in text:
            sep = "" if not text or text.endswith("\n") else "\n"
            f.write_text(f"{text}{sep}\n{header}\ntrust_level = \"trusted\"\n", encoding="utf-8")
        return [str(wd)]
    raise ValueError(agent)


# ---------------------------------------------------------------- 期待する部品
def _frontmatter_name(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"name:\s*['\"]?([^'\"#]+?)['\"]?\s*$", line)
        if m:
            return m.group(1).strip()
    return None


def _skills(base: Path) -> list[tuple[str, str]]:
    """(frontmatter の名前, フォルダ名)。名前がなければフォルダ名。"""
    if not base.is_dir():
        return []
    return [(_frontmatter_name(f) or f.parent.name, f.parent.name) for f in sorted(base.glob("*/SKILL.md"))]


def _agents(base: Path) -> list[tuple[str, str]]:
    if not base.is_dir():
        return []
    return [(_frontmatter_name(f) or f.stem, f.stem) for f in sorted(base.glob("*.md"))]


def _json_mcp(f: Path) -> list[tuple[str, str]]:
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    servers = d.get("mcpServers") if isinstance(d, dict) else None
    return [(str(k), str(k)) for k in servers] if isinstance(servers, dict) else []


_TOML_MCP = re.compile(r'^\s*\[mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\]\s*$', re.MULTILINE)


def _toml_mcp(f: Path) -> list[tuple[str, str]]:
    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [(n, n) for n in dict.fromkeys(a or b for a, b in _TOML_MCP.findall(text))]


def expected_parts(agent: str, home: Path, config_dir: Path, workdir: Path, trusted: bool) -> dict[str, list[tuple[str, str]]]:
    """CLI の探索場所（REDESIGN.md §6.2 の表）にある部品。値は (名前, 別名) の並び（照合はどちらか一致で読み込み済み）。
    instructions は (試行フォルダからの相対パス, 先頭の非空行)。"""
    H, C, W = Path(home), Path(config_dir), Path(workdir)
    root = H.parent

    def instr(*files: Path) -> list[tuple[str, str]]:
        out = []
        for f in files:
            if f.is_file():
                first = next((ln.strip() for ln in f.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()), "")
                out.append((_rel(f, root), first))
        return out

    if agent == "claude_code":
        return {"skills": _skills(C / "skills") + _skills(W / ".claude" / "skills"),
                "agents": _agents(C / "agents") + _agents(W / ".claude" / "agents"),
                "mcp_servers": _json_mcp(C / ".claude.json") + _json_mcp(W / ".mcp.json"),
                "instructions": instr(C / "CLAUDE.md", W / "CLAUDE.md", W / ".claude" / "CLAUDE.md")}
    if agent == "codex":
        def agents_md(d: Path) -> Path:
            return d / "AGENTS.override.md" if (d / "AGENTS.override.md").is_file() else d / "AGENTS.md"
        return {"skills": _skills(C / "skills") + _skills(H / ".agents" / "skills") + _skills(W / ".agents" / "skills"),
                "agents": [],
                "mcp_servers": _toml_mcp(C / "config.toml") + (_toml_mcp(W / ".codex" / "config.toml") if trusted else []),
                "instructions": instr(agents_md(C), agents_md(W))}
    raise ValueError(agent)


def misplaced(agent: str, home: Path, config_dir: Path, isolate_home: bool) -> list[str]:
    """CLI が読まない場所に置かれた設定（実測で確かめたものだけ）。"""
    out = []
    if agent == "claude_code" and isolate_home and (Path(home) / ".claude.json").is_file() and Path(config_dir) != Path(home):
        out.append("{home}/.claude.json は CLAUDE_CONFIG_DIR を設定すると読まれません（MCP などは {config_dir}/.claude.json に置く）")
    return out


# ---------------------------------------------------------------- 置いたファイル
def _rel(p: Path, root: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _sha(f: Path) -> str | None:
    try:
        return hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def installed_files(home: Path, workdir: Path, fixture_snapshot: dict[str, str], workdir_snapshot: dict[str, str]) -> dict[str, Any]:
    """一時ホームのファイル（認証ファイルを除く）と、install が作業ディレクトリに足した・変えたファイル。パスは試行フォルダからの相対。"""
    root = Path(home).parent
    files: dict[str, str | None] = {}
    for f in sorted(Path(home).rglob("*")):
        if f.is_file() and f.name not in CREDENTIAL_FILES:
            files[_rel(f, root)] = _sha(f)
    for rel, h in sorted(workdir_snapshot.items()):
        if fixture_snapshot.get(rel) != h:
            files[_rel(Path(workdir) / rel, root)] = _sha(Path(workdir) / rel)
    out: dict[str, Any] = dict(list(files.items())[:MAX_INSTALLED])
    if len(files) > MAX_INSTALLED:
        out["…"] = f"ほか {len(files) - MAX_INSTALLED} 件"
    return out


def redact_argv(argv: list[str]) -> list[str]:
    return [_SECRET_ARG.sub(r"\1***", a) for a in argv]


# ---------------------------------------------------------------- 観測
_SKILL_LINE = re.compile(r"^- ([^:\n]+?): .*\(file: [^)\n]*SKILL\.md\)\s*$", re.MULTILINE)


def parse_prompt_input(text: str) -> dict[str, Any] | None:
    """`codex debug prompt-input` の JSON から、モデルに見えるスキルの一覧と AGENTS.md の本文を取り出す（0.169.0 で確認した形）。"""
    try:
        items = json.loads(text)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    texts = [str(c.get("text", "")) for it in items if isinstance(it, dict)
             for c in (it.get("content") or []) if isinstance(c, dict)]
    skills: list[str] = []
    for t in texts:
        if "<skills_instructions>" in t:
            skills += [m.strip() for m in _SKILL_LINE.findall(t)]
    instructions = "\n".join(t for t in texts if "AGENTS.md instructions" in t)
    return {"skills": list(dict.fromkeys(skills)), "instructions_text": instructions}


def _probe_args(extra_args: list[str]) -> list[str]:
    """設定を変える追加引数（-c・--config・--enable・--disable・-p・--profile）だけを観測にも渡す。"""
    out, i = [], 0
    while i < len(extra_args):
        a = extra_args[i]
        if a in ("-c", "--config", "--enable", "--disable", "-p", "--profile") and i + 1 < len(extra_args):
            out += [a, extra_args[i + 1]]
            i += 2
            continue
        if a.split("=")[0] in ("--config", "--enable", "--disable", "--profile"):
            out.append(a)
        i += 1
    return out


def observe_codex(exe: list[str], env: dict[str, str], cwd: Path, extra_args: list[str], timeout_s: int = 60) -> dict[str, Any] | None:
    """Codex が読み込む部品を、試行と同じ環境で費用 0 のコマンドから観測する（モデルは呼ばない）。観測できなければ None。"""
    extra = _probe_args(extra_args)

    def run(args: list[str]) -> str | None:
        try:
            p = subprocess.run(exe + args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout_s,
                               stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout if p.returncode == 0 else None

    pi = run(["debug", "prompt-input"] + extra + ["x"])
    parsed = parse_prompt_input(pi) if pi is not None else None
    if parsed is None:
        return None
    mcp_out = run(["mcp", "list", "--json"] + extra)
    mcp: list[str] | None = None
    if mcp_out is not None:
        try:
            mcp = [str(s.get("name")) for s in json.loads(mcp_out) if isinstance(s, dict) and s.get("enabled", True)]
        except (ValueError, TypeError, AttributeError):
            mcp = None
    return {"source": "prompt_input", "skills": parsed["skills"], "agents": None, "mcp_servers": mcp,
            "instructions_text": parsed["instructions_text"]}


def observed_from_init(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Claude Code の system/init（アダプタが meta に入れたもの）から。init がなければ None。"""
    if "loaded_skills" not in meta:
        return None
    return {"source": "init", "skills": list(meta.get("loaded_skills") or []),
            "agents": [str(a) for a in meta.get("loaded_agents") or []],
            "mcp_servers": list(meta.get("mcp_server_names") or []), "instructions_text": None}


# ---------------------------------------------------------------- 照合と記録
def missing_parts(expected: dict[str, list[tuple[str, str]]], observed: dict[str, Any] | None) -> dict[str, list[str] | None] | None:
    """期待したのに読み込まれなかった部品。観測できなかった種類は None（空とは区別する）。"""
    if observed is None:
        return None
    out: dict[str, list[str] | None] = {}
    for kind in ("skills", "agents", "mcp_servers"):
        seen = observed.get(kind)
        if seen is None:
            out[kind] = None
            continue
        names = {str(s) for s in seen}
        # Claude Code はプラグイン由来を "plugin:skill" と出すので末尾でも一致とみなす
        out[kind] = [n for n, alias in expected.get(kind, [])
                     if not ({n, alias} & names or any(s.endswith(":" + n) for s in names))]
    text = observed.get("instructions_text")
    out["instructions"] = None if text is None else [rel for rel, first in expected.get("instructions", []) if first and first not in text]
    return out


def trust_warnings(stderr: str) -> list[str]:
    return [ln.strip()[:300] for ln in stderr.splitlines() if "not been trusted" in ln or "not trusted" in ln][:10]


def record(*, agent: str, cli_version: str | None, argv: list[str], isolate_home: bool, config_env: str,
           env_set: list[str], env_unset: list[str], route_vars: list[str], trust_paths: list[str], trust_enabled: bool,
           installed: dict[str, Any], expected: dict[str, list[tuple[str, str]]], observed: dict[str, Any] | None,
           misplaced_notes: list[str], stderr: str) -> dict[str, Any]:
    """effective_config.json と TrialRecord.effective の中身（REDESIGN.md §6.2 の形）。"""
    obs = None
    if observed is not None:
        obs = {k: observed.get(k) for k in ("source", "skills", "agents", "mcp_servers")}
        obs["instructions"] = None if observed.get("instructions_text") is None else "observed"
    return {
        "schema": SCHEMA, "agent": agent, "cli_version": cli_version, "argv": redact_argv(argv),
        "isolate_home": isolate_home, "config_env": config_env,
        "env_set": sorted(env_set), "env_unset": sorted(env_unset), "route_vars": sorted(route_vars),
        "trust": {"enabled": trust_enabled, "paths": trust_paths},
        "installed": installed,
        "expected": {k: [n for n, _ in expected.get(k, [])] for k in KINDS},
        "observed": obs,
        "missing": missing_parts(expected, observed),
        "misplaced": misplaced_notes,
        "warnings": trust_warnings(stderr),
    }


def has_missing(effective: dict[str, Any] | None) -> bool:
    m = (effective or {}).get("missing")
    return bool(m) and any(v for v in m.values())
