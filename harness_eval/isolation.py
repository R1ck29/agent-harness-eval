"""試行ごとの隔離環境（一時ホーム・作業ディレクトリ）の準備と、プロセス実行。

試行ディレクトリの構成:
    <trial_dir>/home/            一時ホーム（isolate_home: true のとき HOME / USERPROFILE をここに向ける）
    <trial_dir>/home/.claude/    Claude Code の CLAUDE_CONFIG_DIR
    <trial_dir>/home/.codex/     Codex の CODEX_HOME
    <trial_dir>/workdir/         フィクスチャを複製した作業ディレクトリ
    <trial_dir>/stdout.jsonl, stderr.txt

Codex は OS の一時ディレクトリ配下の CODEX_HOME だと補助バイナリの作成を拒否する
（0.163.0 で警告を確認）。そのため試行ディレクトリは結果ディレクトリ配下に作る。
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .spec import Profile

# 親プロセス（Claude Code の中から実行した場合など）から受け継ぐと挙動が変わる変数
INHERITED_UNSET = [
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SSE_PORT", "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "CODEX_HOME",
]


@dataclass
class TrialPaths:
    root: Path
    home: Path
    config_dir: Path
    workdir: Path
    stdout: Path
    stderr: Path


def make_trial_paths(root: Path, config_dirname: str) -> TrialPaths:
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    home = root / "home"
    cfg = home / config_dirname
    wd = root / "workdir"
    for d in (home, cfg, wd):
        d.mkdir(parents=True, exist_ok=True)
    return TrialPaths(root, home, cfg, wd, root / "stdout.jsonl", root / "stderr.txt")


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
    env = dict(os.environ)
    for k in INHERITED_UNSET + profile.env_unset:
        env.pop(k, None)
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
    """copy_credentials: true のとき、本物の認証ファイルを一時ホームに複製する（試行後に削除）。"""
    real = Path.home()
    cands = {"claude_code": [(Path(os.environ.get("CLAUDE_CONFIG_DIR", real / ".claude")) / ".credentials.json", tp.config_dir / ".credentials.json")],
             "codex": [(Path(os.environ.get("CODEX_HOME", real / ".codex")) / "auth.json", tp.config_dir / "auth.json")]}
    for src, dst in cands.get(agent, []):
        if src.is_file():
            shutil.copy2(src, dst)


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
