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

import hashlib
import os
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import credentials
from .spec import ROUTE_VARS, Profile

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
    lent: list = field(default_factory=list)  # 複製した認証ファイル（credentials.Lent）。試行のあと release_home で返す


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
    real_home = Path.home()
    if profile.isolate_home:
        env["HOME"] = str(tp.home)
        env["USERPROFILE"] = str(tp.home)
        # ホームを差し替えても AWS（Bedrock）の認証情報ファイルは本物を参照させる
        for var, rel in (("AWS_SHARED_CREDENTIALS_FILE", ".aws/credentials"), ("AWS_CONFIG_FILE", ".aws/config")):
            if var not in env and (real_home / rel).is_file():
                env[var] = str(real_home / rel)
    env[config_env] = str(tp.config_dir)
    ph = placeholders(tp, profile)
    for k, v in profile.env.items():
        env[k] = fill(v, ph)
    return env


def copy_credentials(agent: str, tp: TrialPaths) -> None:
    """copy_credentials: true のとき、本物の認証ファイルを一時ホームに複製する（ロックの中で。試行のあと release_home で返す）。"""
    lent = credentials.lend(agent, tp.config_dir)
    if lent is not None:
        tp.lent.append(lent)


def release_home(tp: TrialPaths, log=None) -> list[str]:
    """試行の一時ホームと一時フォルダを消す。その前に、試行の中で更新された認証ファイルを本物へ書き戻す（ロック付き・原子的）。
    戻り値: 起きたこと（"written_back" / "conflict"）の一覧。"""
    events = []
    for lent in tp.lent:
        try:
            ev = credentials.give_back(lent)
        except OSError as e:
            ev = f"error: {type(e).__name__}"
        if ev:
            events.append(ev)
            if log:
                log(f"  注意: 試行の中で {lent.agent} の認証が更新されました → "
                    + ("本物へ書き戻しました" if ev == "written_back" else f"本物へは書き戻していません（{ev}。本物が先に更新されていました）"))
    tp.lent.clear()
    shutil.rmtree(tp.home, ignore_errors=True)
    if tp.tmp is not None:
        shutil.rmtree(tp.tmp, ignore_errors=True)
    return events


# ---------------------------------------------------------------- 隔離（bubblewrap）
# 見えるもの = 試行ディレクトリ（一時ホーム・作業ディレクトリ。書ける）、読み取り専用のシステム、エージェントの CLI の本体、
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
    args = ["bwrap", "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/home", "--tmpfs", "/workspace"]
    for p in dict.fromkeys(ro):
        args += ["--ro-bind", p, p]
    trial = str(tp.root.resolve())
    args += ["--bind", trial, trial, "--chdir", str(tp.workdir.resolve())]
    for k in ("TMPDIR", "TEMP", "TMP"):
        args += ["--setenv", k, "/tmp"]
    run = real if not _visible(exe, ro + [trial]) else exe
    return args + ["--", run, *cmd[1:]]


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
    out = {}
    for f in d.rglob("*"):
        if f.is_file():
            try:
                out[f.relative_to(d).as_posix()] = hashlib.sha1(f.read_bytes()).hexdigest()
            except OSError:
                pass
    return out


def _kill_tree(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


_RUNNING: set = set()
_RUNNING_LOCK = threading.Lock()


def kill_running() -> None:
    """実行中のエージェント（子孫プロセスを含む）をすべて止める。Ctrl-C で中断したときに使う。"""
    with _RUNNING_LOCK:
        procs = list(_RUNNING)
    for p in procs:
        _kill_tree(p)


@dataclass
class ProcResult:
    returncode: int | None
    timed_out: bool
    wall_ms: int
    launch_error: str | None = None


def run_process(cmd: list[str], cwd: Path, env: dict[str, str], stdin_text: str | None,
                stdout_path: Path, stderr_path: Path, timeout_s: int) -> ProcResult:
    """stdout/stderr はファイルに直接書く（大量出力でもパイプが詰まらない）。
    タイムアウト時は子プロセスごと終了させる。"""
    t0 = time.monotonic()
    kw: dict = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        try:
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=err,
                                 stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL, **kw)
        except OSError as e:
            return ProcResult(None, False, 0, launch_error=f"{type(e).__name__}: {e}")
        with _RUNNING_LOCK:
            _RUNNING.add(p)
        try:
            # communicate は標準入力の書き込みとタイムアウトを同時に扱う（子が入力を読まなくても詰まらない）
            p.communicate(stdin_text.encode("utf-8") if stdin_text is not None else None, timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(p)
            timed_out = True
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(p)
    return ProcResult(p.returncode, timed_out, int((time.monotonic() - t0) * 1000))
