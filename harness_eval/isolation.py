"""試行ごとの隔離環境（一時ホーム・作業ディレクトリ）の準備と、プロセス実行。

試行ディレクトリの構成:
    <trial_dir>/home/            一時ホーム（isolate_home: true のとき HOME / USERPROFILE をここに向ける）
    <trial_dir>/home/.claude/    Claude Code の CLAUDE_CONFIG_DIR
    <trial_dir>/home/.codex/     Codex の CODEX_HOME
    <trial_dir>/workdir/         フィクスチャを複製した作業ディレクトリ
    <trial_dir>/tmp/             試行の一時フォルダ（TMPDIR・TEMP・TMP。試行のあとに消す。bwrap の中では空の /tmp）
    <trial_dir>/stdout.jsonl, stderr.txt

環境変数は許可リスト: 親の環境から渡すのは BASE_ENV（PATH・LANG・LC_*・TERM など、OS とネットワークに要るもの）と、
プロファイルの inherit_env に名前を書いたものだけ。それ以外（API キー・GH_TOKEN・親のセッションの変数など）は渡さない。

Codex は OS の一時ディレクトリ配下の CODEX_HOME だと補助バイナリの作成を拒否する
（0.163.0 で警告を確認）。そのため試行ディレクトリは結果ディレクトリ配下に作る。
"""
from __future__ import annotations

from contextlib import ExitStack
import errno
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable

from . import credentials
from .spec import ROUTE_VARS, Profile

log = logging.getLogger(__name__)

# 親プロセス（Claude Code・デスクトップアプリ・Agent SDK の中から実行した場合など）が子に渡すセッションの変数。
# 受け継ぐと、同じプロファイルでも起動元で挙動が変わる（入れ子扱い・推論の強さ・簡易モード・認証の更新・親アプリへの通信など）。
# CLI の実行ファイルに名前が含まれることを確認したもの（2.1.295）。CLAUDE で始まる変数には組織で意図して設定するもの
# （クライアント証明書など）もあるので、接頭辞ではまとめて消さない。ここにない変数は doctor が名前を表示する
INHERITED_UNSET = [
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SSE_PORT", "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "CODEX_HOME",
    "CLAUDE_EFFORT", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_OAUTH_SCOPES", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
    "CLAUDE_CODE_ACCOUNT_UUID", "CLAUDE_CODE_ORGANIZATION_UUID", "CLAUDE_CODE_USER_EMAIL",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_TERMINAL_MCP_TOOLS", "CLAUDE_CODE_REPORT_FINDINGS",
    "CLAUDE_CODE_DESKTOP_APP_VERSION", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_CODE_EMIT_STARTUP_TIMING", "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES", "CLAUDE_CODE_EAGER_FLUSH",
    "CLAUDE_CODE_STARTUP_FAILURE_RESULTS", "CLAUDE_CODE_DISABLE_CRON", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
    "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_AGENT_SDK_MCP_NO_PREFIX", "CLAUDE_PID",
]


# 親の環境から試行にそのまま渡す変数（許可リスト）。OS の基本・端末・言語と、モデルの API に届くためのネットワークの設定
# （プロキシ・社内の証明書。値に評価の中身はない）。Windows の名前は大文字小文字を区別しないので大文字で比べる
BASE_ENV = frozenset({
    "PATH", "PATHEXT", "LANG", "LANGUAGE", "TERM", "COLORTERM", "TZ", "SHELL", "USER", "LOGNAME", "USERNAME",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "APPDATA", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO",  # 文字コード（Windows の cp1252 で日本語や記号を出して落ちないように）
})
# 渡さないときに doctor が名前を出す変数（そのエージェントの経路・認証・設定になりうるもの。GH_TOKEN などは渡さないのが目的なので出さない）
_NOTABLE = {"claude_code": ("ANTHROPIC_", "CLAUDE", "AWS_", "GOOGLE_", "CLOUD_ML_", "VERTEX_"), "codex": ("CODEX_", "OPENAI_", "AZURE_")}


def base_allowed(name: str) -> bool:
    u = name.upper()
    return u in BASE_ENV or u.startswith("LC_")


def route_variables(profile: Profile, environ: dict[str, str]) -> tuple[list[str], list[str]]:
    """(試行で使うと決めた経路の変数, 親の環境にあるが宣言していないので試行に渡さない経路の変数)。名前だけを返す（値は扱わない）。
    決めた = プロファイルの env にある、または inherit_env で宣言して親の環境にある。"""
    names = ROUTE_VARS[profile.agent]
    dropped = set(INHERITED_UNSET) | set(profile.env_unset)
    present = [n for n in names if n in profile.env or (environ.get(n) and n not in dropped)]
    chosen = [n for n in present if n in profile.env or n in profile.inherit_env]
    return chosen, [n for n in present if n not in chosen]


_SECRET = ("KEY", "TOKEN", "HEADERS")  # 名前にこれを含む変数は秘密として扱い、値をキャッシュキーにも使わない


def parent_env_names() -> list[str]:
    """親の環境の変数の名前すべて（評価対象ではない補助のエージェント = 判定役・書き換え役が、借りるプロファイルなしで動くとき用）。"""
    return sorted(n for n in os.environ if n not in INHERITED_UNSET)


def missing_inherited(profile: Profile, environ: dict[str, str]) -> list[str]:
    """inherit_env で宣言したのに試行に渡らない変数の名前（親の環境にない・空・env_unset で消す。env で決めたものは除く）。
    API キーやトークンなら、全試行がログインできずに終わる。"""
    dropped = set(INHERITED_UNSET) | set(profile.env_unset)
    # 経路の変数と秘密（キー・トークン）だけを見る。その他の名前（証明書の場所など）は、なければ渡らないだけ
    watch = set(ROUTE_VARS[profile.agent])
    return [n for n in profile.inherit_env if n not in profile.env and (n in watch or any(x in n for x in _SECRET))
            and (not environ.get(n) or n in dropped)]


def dropped_vars(profile: Profile, environ: dict[str, str]) -> list[str]:
    """親の環境にあるが許可リストにも inherit_env にもないので試行に渡さない、エージェントの設定になりうる変数（名前だけ。
    経路の変数は route_variables が別に出す）。組織で意図して設定したもの（クライアント証明書など）なら inherit_env に書く。"""
    skip = set(INHERITED_UNSET) | set(profile.env_unset) | set(profile.env) | set(profile.inherit_env) | set(ROUTE_VARS[profile.agent])
    return sorted(n for n in environ if n.upper().startswith(_NOTABLE[profile.agent]) and n not in skip and not base_allowed(n))


def route_signature(profile: Profile, environ: dict[str, str]) -> str:
    """試行に渡る経路の変数のキャッシュキー用の要約。経路を変えれば再実行される。
    秘密（キー・トークン・ヘッダ）は名前だけ。秘密でないもの（モデルの別名の実体・接続先・切り替え）は値の短いハッシュも含め、
    値だけを変えても再実行されるようにする（値そのものは残さない）。"""
    chosen, _ = route_variables(profile, environ)
    dropped = set(INHERITED_UNSET) | set(profile.env_unset)
    extra = [n for n in profile.inherit_env if n not in chosen and environ.get(n) and n not in dropped]
    parts = []
    for name in sorted(chosen + extra):
        if any(s in name for s in _SECRET):
            parts.append(name)
            continue
        value = profile.env.get(name, environ.get(name, ""))
        parts.append(f"{name}#{hashlib.sha256(value.encode()).hexdigest()[:8]}")
    return ",".join(parts)


# 試行の作業ディレクトリ（--out の下）より上のフォルダにあると、一時ホームにしても CLI が読み込んでしまう指示・スキル
# （最終レビューの実測: Claude Code 2.1.295 は上位の CLAUDE.md と .claude/ の skills・rules・agents・commands を、Codex 0.146.1 は
# git ルートまでの AGENTS.md を読んだ）。配布物なしの基準にも入り Δ を壊すため、見つかれば run は止める。
# 安全側に、エージェント設定のフォルダ（.claude/・.codex/・.agents/）は中身を問わず、同じ系統のファイル名も見る
ANCESTOR_INSTRUCTIONS = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md", ".mcp.json", ".claude", ".codex", ".agents")


def ancestor_instructions(out_dir: Path) -> list[str]:
    """--out とその上のフォルダにある、試行に読み込まれうる指示・スキルのパス。"""
    p = Path(out_dir).resolve()
    return [str(d / rel) for d in (p, *p.parents) for rel in ANCESTOR_INSTRUCTIONS if (d / rel).exists()]


@dataclass
class TrialPaths:
    root: Path
    home: Path
    config_dir: Path
    workdir: Path
    stdout: Path
    stderr: Path
    tmp: Path | None = None
    lent: list = field(default_factory=list)  # 複製した認証ファイル（credentials.Lent）。試行のあと release_home で捨てる（書き戻さない）


def make_trial_paths(root: Path, config_dirname: str) -> TrialPaths:
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    home = root / "home"
    cfg = home / config_dirname
    wd = root / "workdir"
    tmp = root / "tmp"
    for d in (home, cfg, wd, tmp):
        d.mkdir(parents=True, exist_ok=True)
    return TrialPaths(root, home, cfg, wd, root / "stdout.jsonl", root / "stderr.txt", tmp)


def placeholders(tp: TrialPaths, profile: Profile) -> dict[str, str]:
    return {"home": str(tp.home), "config_dir": str(tp.config_dir), "workdir": str(tp.workdir),
            "profile_dir": str(profile.base_dir), "python": sys.executable}


def fill(s: str, ph: dict[str, str], quote: bool = False) -> str:
    out = s
    for k, v in ph.items():
        val = (shlex.quote(v) if os.name != "nt" else f'"{v}"') if quote else v
        out = out.replace("{" + k + "}", val)
    return out


def build_env(profile: Profile, tp: TrialPaths, config_env: str) -> dict[str, str]:
    """試行の環境変数。親の環境からは許可リスト（BASE_ENV）と inherit_env に書いたものだけを渡す。"""
    dropped = set(INHERITED_UNSET) | set(profile.env_unset)
    env = {k: v for k, v in os.environ.items() if base_allowed(k) and k not in dropped}
    for k in profile.inherit_env:
        if k in os.environ and k not in dropped:
            env[k] = os.environ[k]
    if tp.tmp is not None:  # 試行の一時ファイルは試行ディレクトリの中に（OS の /tmp に残さない。試行のあとに消す）
        for k in ("TMPDIR", "TEMP", "TMP"):
            env[k] = str(tp.tmp)
    if profile.isolate_home:
        env["HOME"] = str(tp.home)
        env["USERPROFILE"] = str(tp.home)
    env[config_env] = str(tp.config_dir)
    ph = placeholders(tp, profile)
    for k, v in profile.env.items():
        env[k] = fill(v, ph)
    bedrock = env.get("CLAUDE_CODE_USE_BEDROCK", "").strip().lower() not in ("", "0", "false")
    if profile.isolate_home and profile.agent == "claude_code" and bedrock:
        # 実行環境で Bedrock が有効なときだけ AWS の設定ファイルを補う。
        real_home = Path.home()
        for var, rel in (("AWS_SHARED_CREDENTIALS_FILE", ".aws/credentials"), ("AWS_CONFIG_FILE", ".aws/config")):
            if var not in env and var not in dropped and (real_home / rel).is_file():
                env[var] = str(real_home / rel)
    return env


def copy_credentials(agent: str, tp: TrialPaths) -> None:
    """copy_credentials: true のとき、本物の認証ファイルを一時ホームに複製する（ロックの中で。試行のあと release_home で捨てる。書き戻さない）。"""
    lent = credentials.lend(agent, tp.config_dir)
    if lent is not None:
        tp.lent.append(lent)


def release_home(tp: TrialPaths, log=None) -> list[str]:
    """試行の一時ホームと一時フォルダを消す。複製した認証は捨てるだけ（本物へは書き戻さない。solver が書ける複製を本物へ戻すと、
    solver が本物のログインを書き換えられるため）。戻り値は常に空（呼び出し側の互換）。"""
    tp.lent.clear()
    shutil.rmtree(tp.home, ignore_errors=True)
    if tp.tmp is not None:
        shutil.rmtree(tp.tmp, ignore_errors=True)
    return []


# ---------------------------------------------------------------- 隔離（bubblewrap）
# 見えるもの = 一時ホーム・作業ディレクトリ（書ける。試行ルートのログ・採点記録は見えない）、読み取り専用のシステム、エージェントの CLI の本体、
# プロファイルの sandbox_ro。--out の他の試行・タスクのフォルダ（参照解・採点）・本物のホーム（認証）・/tmp の残りは見えない。
# ネットワークはモデルの API のために残す（上流への参照は leak 検査が INVALID にする）。agent-verify-layer の
# eval/tools/sandbox-run.sh と同じ見え方
SANDBOX_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt")
_BWRAP_PROBLEM: dict[str, str | None] = {}


def sandbox_mode(profile: Profile) -> str:
    """"bwrap"（隔離する）/ "off"（プロファイルで切った）/ "unsupported"（Linux 以外。leak 検査だけ）。"""
    if profile.sandbox == "off":
        return "off"
    return "bwrap" if platform.system() == "Linux" else "unsupported"


def bwrap_problem() -> str | None:
    """bwrap が使えなければ理由（なし・ユーザー名前空間が使えないなど）。結果は 1 回の実行の中で使い回す。"""
    if "r" not in _BWRAP_PROBLEM:
        exe = shutil.which("bwrap")
        if not exe:
            _BWRAP_PROBLEM["r"] = "bwrap がありません（apt install bubblewrap など）"
        else:
            try:
                p = subprocess.run([exe, "--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
                                    "--", "true"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
                _BWRAP_PROBLEM["r"] = None if p.returncode == 0 else f"bwrap が動きません: {p.stderr.strip()[:200]}"
            except (OSError, subprocess.TimeoutExpired) as e:
                _BWRAP_PROBLEM["r"] = f"bwrap が動きません: {e}"
    return _BWRAP_PROBLEM["r"]


def _visible(path: str, binds: list[str]) -> bool:
    return any(path == b or path.startswith(b.rstrip("/") + "/") for b in binds)


def sandbox_cmd(cmd: list[str], tp: TrialPaths, env: dict[str, str], profile: Profile, n_exe: int = 1) -> list[str]:
    """cmd（先頭 n_exe 個が実行ファイルとその引数のスクリプト）を bwrap の中で動かすコマンドにする。"""
    exe = shutil.which(cmd[0], path=env.get("PATH")) or cmd[0]
    real = os.path.realpath(exe)
    ro: list[str] = [p for p in SANDBOX_SYSTEM if os.path.exists(p)]
    if "/node_modules/" in real:  # CLI の本体（node_modules。秘密は置かれない）
        ro.append(real.split("/node_modules/")[0] + "/node_modules")
    elif os.path.basename(real).startswith("python"):  # Python の本体と標準ライブラリ（venv の外の実体）
        ro.append(str(Path(real).parents[1]))
    else:
        ro.append(real)
    for a in cmd[1:n_exe]:  # executable に書いたスクリプト（[node, cli.js] など）
        if os.path.isabs(a) and os.path.isfile(a):
            ro.append(os.path.realpath(a))
    for var in ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"):
        if env.get(var) and os.path.isfile(env[var]):
            ro.append(env[var])
    resolv = "/etc/resolv.conf"  # systemd-resolved では /run の下へのリンク（/run は空にするので実体を見せる）
    if os.path.islink(resolv) and os.path.realpath(resolv).startswith("/run/"):
        ro.append(os.path.realpath(resolv))
    ph = placeholders(tp, profile)
    ro += [fill(x, ph) for x in profile.sandbox_ro if os.path.exists(fill(x, ph))]
    args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/home", "--tmpfs", "/workspace"]
    for p in dict.fromkeys(ro):
        args += ["--ro-bind", p, p]
    # solver が書ける場所は一時ホームと作業ディレクトリだけ（TMPDIR は隔離の中の /tmp）。標準出力・標準エラーのログ・採点記録・
    # 費用記録のある試行ルート（tp.root）は見せない・書かせない（ログを書き換えて合否・費用・leak 判定を操作させない）
    writable = [str(tp.home.resolve()), str(tp.workdir.resolve())]
    for w in writable:
        args += ["--bind", w, w]
    args += ["--chdir", str(tp.workdir.resolve())]
    for k in ("TMPDIR", "TEMP", "TMP"):
        args += ["--setenv", k, "/tmp"]
    run = real if not _visible(exe, ro + writable) else exe
    return args + ["--", run, *cmd[1:]]


# ---------------------------------------------------------------- command グレーダーの隔離
# 採点は候補のコード（実装・テスト）を import / 実行しうる。solver と同等以上に閉じる: 実のホーム・他の試行・採点に要らない評価資材・
# 認証・ネットワークを見せず、作業フォルダは読み取り専用、環境変数は許可リストだけ。bwrap がない OS では「隔離なし」を記録する
# （verdict が decisive では結論を出さない）。
_GRADER_ENV_DROP = ("PROXY", "CERT", "_CA_", "CA_BUNDLE", "SSL_")


def grader_isolation() -> str:
    """"bwrap"（採点を隔離して実行できる）/ "none"（Linux 以外・bwrap が使えない。候補のコードがホストの権限で動く）。"""
    return "bwrap" if sys.platform.startswith("linux") and bwrap_problem() is None else "none"


def grader_env(workdir: Path, tmp_dir: Path, record_path: Path) -> dict[str, str]:
    """採点コマンドの環境変数: 許可リスト（BASE_ENV。プロキシ・証明書は除く）+ TRIAL_*・一時フォルダ。HOME は採点の一時フォルダ。"""
    env = {k: v for k, v in os.environ.items() if base_allowed(k) and not any(m in k.upper() for m in _GRADER_ENV_DROP)}
    env.update({"TRIAL_WORKDIR": str(workdir), "TRIAL_RECORD": str(record_path), "HOME": str(tmp_dir), "USERPROFILE": str(tmp_dir),
                "TMPDIR": str(tmp_dir), "TEMP": str(tmp_dir), "TMP": str(tmp_dir)})
    return env


def _grader_material(cmd: str, extra_ro: list[str], workdir: Path, resolved_workdir: Path) -> tuple[list[str], list[str]]:
    """採点コマンドが参照する評価資材（採点スクリプト・正解データなど。コマンドに書かれた実在する絶対パスと、明示した ro）と、
    `{task_dir}/../checks/x.py` のように `..` で戻る道筋のために隔離の中に作る空のフォルダ（中身は見せない）。"""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    home = Path.home().resolve()
    out: list[str] = []
    dirs: list[str] = []
    workdirs = {Path(os.path.abspath(workdir)), resolved_workdir}
    for t in [*toks, *extra_ro]:
        path = Path(t)
        if not path.is_absolute():
            continue
        if path.anchor == "//":
            path = Path("/") / path.relative_to("//")
        # Check each prefix before traversing it, including paths that leave the workdir with "..".
        r = Path(path.anchor)
        parents_to_create: list[str] = []
        mode = stat.S_IFDIR
        for part in path.parts[1:]:
            if not stat.S_ISDIR(mode):
                break
            if part == "..":
                parents_to_create.append(str(r))
                r = r.parent
            else:
                r = r / part
            if any(r.is_relative_to(w) for w in workdirs):
                break
            try:
                mode = r.lstat().st_mode
            except OSError:
                break
            if stat.S_ISLNK(mode):  # solver が差し替えられる道筋かもしれないので bind しない（安全側）。捨てたことは警告する
                log.warning("採点資材の道筋にシンボリックリンクがあるため隔離の中へ見せません: %s（リンク: %s）。"
                            "実体の絶対パスで書くと見せられます", t, r)
                break
        else:
            if r != Path(r.anchor) and r != home and r not in home.parents:
                out.append(str(r))
                dirs.extend(parents_to_create)
    return out, dirs


def _link_chain(path: str) -> list[str]:
    """path と、その道筋にあるシンボリックリンク（path 自身がリンクならその向き先の道筋も）。"""
    out: list[str] = []
    cur = path
    for _ in range(8):
        for a in (Path(cur), *Path(cur).parents):
            if a.is_symlink() and str(a) not in out:
                out.append(str(a))
        if not os.path.islink(cur):
            break
        t = os.readlink(cur)
        cur = t if os.path.isabs(t) else os.path.join(os.path.dirname(cur), t)
    return out


def grader_sandbox_cmd(cmd: str, workdir: Path, tmp_dir: Path, extra_ro: list[str] | None = None) -> list[str]:
    """採点コマンド（シェル文字列）を bwrap の中で動かすコマンドにする。"""
    ro = [p for p in SANDBOX_SYSTEM if os.path.exists(p)]
    py = [sys.executable, sys.prefix, sys.base_prefix]
    home = Path.home().resolve()
    for prefix in (*py, str(Path(os.path.realpath(sys.executable)).parents[1])):  # Python 本体と venv（秘密は置かれない）
        if Path(prefix).resolve() != home:
            ro.append(os.path.realpath(prefix))  # 実体のパスで bind する（リンクの名前では bind しない）
    wd, tmp = str(workdir.resolve()), str(tmp_dir.resolve())
    material, dirs = _grader_material(cmd, extra_ro or [], workdir, Path(wd))
    ro += material
    ro = [p for p in dict.fromkeys(ro) if p not in (wd, tmp)]
    args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/home", "--tmpfs", "/workspace"]
    for p in ro:
        args += ["--ro-bind", p, p]
    args += ["--ro-bind", wd, wd, "--bind", tmp, tmp]
    for d in dict.fromkeys(dirs):
        args += ["--dir", d]
    bound = [p for p in ro + [wd, tmp]]
    links = [lk for src in py for lk in _link_chain(src)
             if not any(lk == b or lk.startswith(b.rstrip("/") + "/") for b in bound) and not lk.startswith(tuple(SANDBOX_SYSTEM))]
    for link in dict.fromkeys(links):  # venv の python は別名のフォルダ（uv の cpython-3.12-… など）を経由する。bind した実体への道筋のリンクを張る
        args += ["--symlink", os.path.realpath(link), link]
    args += ["--chdir", wd]
    return args + ["--", "/bin/sh", "-c", cmd]


def run_install(profile: Profile, tp: TrialPaths, env: dict[str, str], timeout_s: int = 300) -> None:
    """プロファイルの install 手順を順に実行する。失敗したら例外（harness_error になる）。"""
    ph = placeholders(tp, profile)
    for i, step in enumerate(profile.install):
        if "copy" in step:
            src = Path(fill(step["copy"], ph))
            src = src if src.is_absolute() else profile.base_dir / src
            dst_s = fill(step["to"], ph)
            # 相対パスは一時ホーム基準。試行ディレクトリの外（普段の ~/.claude など）には書かせない
            dst = Path(dst_s) if Path(dst_s).is_absolute() else tp.home / dst_s
            if not dst.resolve().is_relative_to(tp.root.resolve()):
                raise RuntimeError(f"install[{i}]: コピー先が試行ディレクトリの外です: {dst}（{{home}}・{{config_dir}}・{{workdir}} を使う）")
            if not src.exists():
                raise RuntimeError(f"install[{i}]: コピー元がありません: {src}")
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                if dst_s.endswith(("/", "\\")) or dst.is_dir():
                    dst = dst / src.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        else:
            cmd = fill(step["run"], ph, quote=True)
            p = subprocess.run(cmd, shell=True, cwd=profile.base_dir, env=env, capture_output=True,
                               text=True, timeout=timeout_s, stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                raise RuntimeError(f"install[{i}] が終了コード {p.returncode}: {cmd}\n{p.stderr[-1000:]}")


def snapshot(d: Path) -> dict[str, str]:
    """作業ディレクトリの {相対パス: ハッシュ}。リンクはたどらない（中身ではなく向き先を記録する）。FIFO などの特殊ファイルは読まない
    （solver が置いたリンク・FIFO で、ホストが本物のファイルを読んだり固まったりしないように）。"""
    out = {}
    for dirpath, _dirs, files in os.walk(d, followlinks=False):
        for name in files + [x for x in _dirs if os.path.islink(os.path.join(dirpath, x))]:
            f = Path(dirpath) / name
            try:
                st = f.lstat()
                if stat.S_ISLNK(st.st_mode):
                    data = b"link:" + os.readlink(f).encode("utf-8", "surrogateescape")
                elif stat.S_ISREG(st.st_mode):
                    data = f.read_bytes()
                else:
                    continue
                out[f.relative_to(d).as_posix()] = hashlib.sha1(data).hexdigest()
            except OSError:
                pass
    return out


def open_new(path: Path, mode: str = "wb"):
    """ホストが書くファイルを開く。リンク・通常ファイル以外には書かない（リンクの先の既存ファイルを壊さない）。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise OSError(f"リンク・特殊ファイルには書きません: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    return os.fdopen(fd, mode, **({} if "b" in mode else {"encoding": "utf-8"}))


def clear_path(path: Path) -> None:
    """solver が置いたかもしれないリンク・特殊ファイル・フォルダを、リンクの先に触れずに取り除く。"""
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def replace_text(path: Path, text: str) -> None:
    """solver が書ける場所へ、ホストがファイルを置く。既にある同名のもの（リンクなど）は先に取り除き、リンクの先は書かない。"""
    clear_path(path)
    with open_new(path, "w") as fh:
        fh.write(text)


def fresh_dir(path: Path) -> Path:
    """solver が書ける場所に、空のフォルダを新しく作る（同名のリンクなどは先に取り除く）。"""
    clear_path(path)
    os.mkdir(path)
    return path


_HAS_WAITID = hasattr(os, "waitid")  # Unix（macOS を除く）。ない OS は従来の poll() で判定する


def _exited_unreaped(p: subprocess.Popen) -> bool:
    """リーダーが終了済みで、まだ回収（wait）されていないか。回収はしない（WNOWAIT）ので、PID（= プロセスグループ ID）はまだ自分のもの。"""
    try:
        return os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:  # すでに回収されている
        return False


def _wait_exit(p: subprocess.Popen, timeout: float) -> None:
    """リーダーの終了を、回収せずに待つ（TimeoutExpired）。回収はあとで p.wait() / _kill_tree() が行う。
    回収の前に killpg できるので、PID が再利用されて無関係なプロセスグループを撃つことがない。"""
    if not _HAS_WAITID or os.name == "nt":
        p.wait(timeout=timeout)
        return
    deadline = time.monotonic() + timeout
    delay = 0.002
    while not _exited_unreaped(p):
        if p.returncode is not None:  # 他で回収された
            return
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(p.args, timeout)
        time.sleep(delay)
        delay = min(delay * 2, 0.05)


def _kill_tree(p: subprocess.Popen) -> None:
    if os.name == "nt":
        if p.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        return
    try:
        if not _HAS_WAITID:  # macOS など: 従来どおり（回収後は PID の再利用を排除できない）
            if p.poll() is not None:
                os.killpg(p.pid, signal.SIGKILL)  # The leader may have exited while descendants still hold its pipes.
                return
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
            return
        if p.returncode is not None:  # 回収済み: グループ ID が別のものに再利用されているかもしれないので、何も送らない
            return
        if not _exited_unreaped(p):
            os.killpg(p.pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while not _exited_unreaped(p) and p.returncode is None and time.monotonic() < deadline:
                time.sleep(0.02)
        if p.returncode is None:
            os.killpg(p.pid, signal.SIGKILL)  # リーダーを回収する前に送る（子孫がパイプを持ち続けていてもここで止める）
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                pass
    except ProcessLookupError:
        p.poll()


_RUNNING: set = set()
_RUNNING_LOCK = threading.Lock()


def kill_running() -> None:
    """実行中のエージェント（子孫プロセスを含む）をすべて止める。Ctrl-C で中断したときに使う。"""
    with _RUNNING_LOCK:
        procs = list(_RUNNING)
    for p in procs:
        _kill_tree(p)


LOG_LIMIT = 16 * 1024 * 1024  # 1 本のログ（stdout / stderr）をディスクへ書く量の上限
_USAGE_LINE_MAX = LOG_LIMIT  # 上限のあとで 1 行として読む最大長（runner._solver_accounting_lines が読む 1 行の上限と同じ）。超えた行は捨てる
# 使用量を載せるイベント行（Claude の result・Codex の turn.completed。adapters.parse(usage_only=True) が読むもの）の足切り。本物かどうかは
# JSON として読んだトップレベルの type で決める（ツールの出力にこの文字列が入っていても、行の枠は使わない）
_USAGE_EVENT = re.compile(rb'"type"\s*:\s*"(result|turn\.completed)"')
_RESULT_SCALARS = ("type", "subtype", "duration_ms", "num_turns", "total_cost_usd", "terminal_reason", "is_error", "api_error_status")
_TURN_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "cache_write_input_tokens", "reasoning_output_tokens")
_Scalar = (str, int, float, bool, type(None))


def _scalars(d: Any, limit: int = 200) -> dict[str, Any]:
    """数値・短い文字列だけの辞書（使用量の中身）。"""
    return {str(k): (v[:limit] if isinstance(v, str) else v) for k, v in (d if isinstance(d, dict) else {}).items() if isinstance(v, _Scalar)}


def _slim_result(e: dict[str, Any]) -> dict[str, Any]:
    """Claude の result 行から、パーサ（adapters.ClaudeAdapter.parse）が使う項目だけを小さく残す（最終応答の長い本文は落とす）。"""
    out: dict[str, Any] = {k: (v[:200] if isinstance(v, str) else v) for k, v in e.items() if k in _RESULT_SCALARS and isinstance(v, _Scalar)}
    if isinstance(e.get("usage"), dict):
        out["usage"] = _scalars(e["usage"])
    if isinstance(e.get("modelUsage"), dict):
        out["modelUsage"] = {str(k): _scalars(v) for k, v in list(e["modelUsage"].items())[:20]}
    if isinstance(e.get("result"), str):
        out["result"] = e["result"][:2000]  # エラーの理由として読まれる冒頭だけ
    if isinstance(e.get("errors"), list):
        out["errors"] = [str(x)[:200] for x in e["errors"][:5]]
    return out


class _UsageKeeper:
    """ログが上限に達したあとの出力から、使用量のイベントだけを集める。1 行ずつ（上限をまたぐ行も、続きを読み切ってから）JSON として読み、
    トップレベルの type が result のものは最新の 1 件（本文を落とす）、turn.completed のものは使用量を足した 1 件にして、末尾に残す。"""

    def __init__(self) -> None:
        self.line = bytearray()
        self.skipping = False  # 今の行は長すぎるので使わない
        self.result: dict[str, Any] | None = None
        self.turns = 0
        self.usage = dict.fromkeys(_TURN_USAGE_KEYS, 0)

    def track(self, chunk: bytes) -> None:
        """上限の前に書いた分: 最後の改行より後ろ（未完の行）だけ持つ。上限で切れた行を、続きと合わせて 1 行として読むため。"""
        i = chunk.rfind(b"\n")
        if i >= 0:
            self.line, self.skipping = bytearray(chunk[i + 1:]), False
        elif not self.skipping:
            self.line += chunk
        if len(self.line) > _USAGE_LINE_MAX:
            self.line.clear()
            self.skipping = True

    def feed(self, chunk: bytes) -> None:
        parts = chunk.split(b"\n")
        for i, part in enumerate(parts):
            if not self.skipping:
                if len(self.line) + len(part) <= _USAGE_LINE_MAX:
                    self.line += part
                else:
                    self.skipping = True
                    self.line.clear()
            if i < len(parts) - 1:  # 1 行が終わった
                self.flush_line()

    def flush_line(self) -> None:
        if not self.skipping and self.line:
            self.take(bytes(self.line))
        self.line.clear()
        self.skipping = False

    def take(self, done: bytes) -> None:
        if not _USAGE_EVENT.search(done):
            return
        try:
            e = json.loads(done)
        except (ValueError, RecursionError):
            return
        if not isinstance(e, dict):
            return
        if e.get("type") == "result":
            self.result = _slim_result(e)
        elif e.get("type") == "turn.completed":
            self.turns += 1
            u = e.get("usage") if isinstance(e.get("usage"), dict) else {}
            for k in _TURN_USAGE_KEYS:
                try:
                    self.usage[k] += int(u.get(k) or 0)
                except (TypeError, ValueError):
                    pass

    def tail(self) -> bytes:
        """ログの末尾に足す行（改行で終わる）。未完の最後の行も読む。"""
        self.flush_line()
        out = []
        if self.turns:
            out.append({"type": "turn.completed", "usage": self.usage})
        if self.result is not None:
            out.append(self.result)
        return b"".join(json.dumps(x).encode("utf-8") + b"\n" for x in out)


@dataclass
class ProcResult:
    returncode: int | None
    timed_out: bool
    wall_ms: int
    launch_error: str | None = None
    log_truncated: bool = False  # ログが上限（log_limit）に達して、以降は使用量の行だけを残した


def run_process(cmd: list[str], cwd: Path, env: dict[str, str], stdin_text: str | None,
                stdout_path: Path, stderr_path: Path, timeout_s: int, log_limit: int = LOG_LIMIT) -> ProcResult:
    """子の stdout/stderr をパイプで読み、親がログへ保存する。タイムアウト時も回収済みの出力を保存する。
    ログは log_limit バイトで打ち切る（ディスクを埋めて使用量の計上や他の書き込みを巻き込まないため）。打ち切ったあともパイプは
    読み続け、使用量のイベント行だけを（_USAGE_TAIL_LIMIT まで）ログの末尾に残す。"""
    t0 = time.monotonic()
    kw: dict = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    with ExitStack() as stack:
        try:
            out = stack.enter_context(open_new(stdout_path))
            err = stack.enter_context(open_new(stderr_path))
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL, **kw)
        except OSError as e:
            return ProcResult(None, False, 0, launch_error=f"{type(e).__name__}: {e}")

        log_lock = threading.Lock()
        stop_writing = False
        errors: list[OSError] = []

        truncated: list[bool] = []

        finalizers: list[Callable[[], None]] = []

        def drain(pipe: BinaryIO, log: BinaryIO) -> None:
            written = 0
            cut = False  # 上限に達した。以降は使用量のイベントだけを残す
            keeper = _UsageKeeper()
            done = False

            def finish() -> None:  # log_lock の中で 1 回だけ。集めた使用量を末尾に書く（タイムアウトで打ち切られる場合も、親が呼ぶ）
                nonlocal done
                if cut and not done:
                    done = True
                    log.write(keeper.tail())
                    log.flush()

            finalizers.append(finish)
            try:
                with pipe:
                    while chunk := os.read(pipe.fileno(), 64 * 1024):
                        with log_lock:
                            if stop_writing:
                                return
                            if not cut:
                                if written + len(chunk) <= log_limit:
                                    log.write(chunk)
                                    log.flush()
                                    written += len(chunk)
                                    keeper.track(chunk)
                                    continue
                                head = chunk[:log_limit - written]
                                log.write(head + b"\n")  # 途中で切れた行は、あとの使用量の行と混ぜない
                                log.flush()
                                cut = True
                                truncated.append(True)
                                keeper.track(head)  # 切れた行は、続きを読み切ってから使用量の行かどうかを決める
                                chunk = chunk[len(head):]
                            keeper.feed(chunk)
                    with log_lock:
                        if not stop_writing:
                            finish()
            except OSError as e:
                errors.append(e)

        def send_input(pipe: BinaryIO, prompt: str) -> None:
            try:
                with pipe:
                    pipe.write(prompt.encode("utf-8"))
            except BrokenPipeError:
                pass
            except OSError as e:
                # Windows may report a closed child stdin as EINVAL.
                if os.name != "nt" or e.errno != errno.EINVAL:
                    errors.append(e)

        assert p.stdout is not None and p.stderr is not None
        workers = [threading.Thread(target=drain, args=(pipe, log), daemon=True)
                   for pipe, log in ((p.stdout, out), (p.stderr, err))]
        if p.stdin is not None and stdin_text is not None:
            workers.append(threading.Thread(target=send_input, args=(p.stdin, stdin_text), daemon=True))

        def wait_workers(deadline: float) -> None:
            for worker in workers:
                worker.join(max(0, deadline - time.monotonic()))
            if any(worker.is_alive() for worker in workers):
                raise subprocess.TimeoutExpired(cmd, timeout_s)

        with _RUNNING_LOCK:
            _RUNNING.add(p)
        timed_out = False
        deadline = time.monotonic() + timeout_s
        try:
            for worker in workers:
                worker.start()
            _wait_exit(p, max(0, deadline - time.monotonic()))
            wait_workers(deadline)
            p.wait()  # 子孫のパイプも閉じた。ここで回収する
        except subprocess.TimeoutExpired:
            _kill_tree(p)
            timed_out = True
            deadline = time.monotonic() + 5
            try:
                _wait_exit(p, max(0, deadline - time.monotonic()))
                wait_workers(deadline)
                p.wait()
            except subprocess.TimeoutExpired:
                # A descendant can retain the pipes. Keep the bytes already written and return.
                _kill_tree(p)
        except BaseException:
            _kill_tree(p)
            raise
        finally:
            with log_lock:
                stop_writing = True
                for finish in finalizers:  # パイプを握る子孫が残っていても、集めた使用量は残す
                    try:
                        finish()
                    except (OSError, ValueError):
                        pass
            with _RUNNING_LOCK:
                _RUNNING.discard(p)
        if errors:
            raise errors[0]
    return ProcResult(p.returncode, timed_out, int((time.monotonic() - t0) * 1000), log_truncated=bool(truncated))
