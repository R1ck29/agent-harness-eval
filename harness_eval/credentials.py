"""copy_credentials: true のときの認証ファイルの貸し出し（複製のみ。本物への書き戻しはしない）。

不具合（2026-11-19）: 試行の中の Codex が複製した auth.json を更新（refresh）すると、refresh token は使い回せないため、
本物の ~/.codex/auth.json の refresh token が使用済みになり、次から「refresh token was already used」で全滅する。

対策:
1. 実行の前に本物の認証ファイルの期限を確かめる（preflight）。アクセストークンの期限が近い・切れている、または最終更新
   （Codex の last_refresh）が古いときは、試行の中で更新が起きるので、実行を止めて利用者に更新を促す
   （`codex login`、または一度 `codex` を実行して更新）。中身は表示しない（期限と時刻だけ）
2. 複製はロックの中で行い、試行のあとは捨てるだけ。**書き戻しはしない**（2026-11-20 のレビュー: 複製は solver が書けるので、
   戻すと solver が本物のログインを書き換えられる。更新が要るなら 1 の事前確認で止め、利用者が `codex login` する）
3. 会社では API キー（Codex: CODEX_API_KEY、Claude: ANTHROPIC_API_KEY）を inherit_env で渡す経路を勧める（更新が起きない）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX だけ。Windows はプロセス内のロックだけ
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

# 試行の中で更新が起きないよう、実行の前に残っていてほしい期限（アクセストークン）と、最終更新の古さの上限
MIN_REMAINING_S = 2 * 3600
MAX_REFRESH_AGE_S = 7 * 86400  # Codex は最終更新が古いと起動時に更新する（日数は版で違いうるので短めに取る）

_FILES = {"claude_code": ("CLAUDE_CONFIG_DIR", ".claude", ".credentials.json"), "codex": ("CODEX_HOME", ".codex", "auth.json")}
_LOCK = threading.Lock()


def source_path(agent: str, environ: dict[str, str] | None = None) -> Path | None:
    """本物の認証ファイルの場所（ファイルがなくても返す）。"""
    if agent not in _FILES:
        return None
    var, dirname, fname = _FILES[agent]
    env = os.environ if environ is None else environ
    return Path(env.get(var) or Path.home() / dirname) / fname


def _jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(data["exp"])
    except Exception:
        return None


def _parse_time(s: Any) -> float | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def inspect(agent: str, path: Path, now: float | None = None) -> dict[str, Any]:
    """期限の情報だけを返す（トークンの値は返さない）。kind: oauth / api_key / missing / unknown。"""
    now = time.time() if now is None else now
    if not path.is_file():
        return {"kind": "missing"}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"kind": "unknown", "reason": "読めない・JSON でない"}
    if not isinstance(d, dict):
        return {"kind": "unknown", "reason": "形式が違う"}
    exp = last = None
    if agent == "codex":
        tokens = d.get("tokens")
        if not tokens and d.get("OPENAI_API_KEY"):
            return {"kind": "api_key"}
        if isinstance(tokens, dict):
            exp = _jwt_exp(str(tokens.get("access_token", "")))
        last = _parse_time(d.get("last_refresh"))
    elif agent == "claude_code":
        o = d.get("claudeAiOauth")
        if isinstance(o, dict) and isinstance(o.get("expiresAt"), (int, float)):
            exp = float(o["expiresAt"]) / 1000
    if exp is None and last is None:
        return {"kind": "unknown", "reason": "期限が書かれていない"}
    return {"kind": "oauth", "expires_at": exp, "remaining_s": None if exp is None else exp - now,
            "last_refresh": last, "refresh_age_s": None if last is None else now - last}


def describe(info: dict[str, Any]) -> str:
    """doctor の表示（値は出さない）。"""
    k = info.get("kind")
    if k == "missing":
        return "認証ファイルがありません（copy_credentials は何も複製しません）"
    if k == "api_key":
        return "API キー（更新は起きません）"
    if k == "unknown":
        return f"期限を確かめられません（{info.get('reason')}）"
    parts = []
    if info.get("expires_at") is not None:
        at = datetime.fromtimestamp(info["expires_at"], timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        parts.append(f"アクセストークンの期限 {at}（あと {info['remaining_s'] / 3600:.1f} 時間）")
    if info.get("last_refresh") is not None:
        parts.append(f"最終更新から {info['refresh_age_s'] / 86400:.1f} 日")
    return "、".join(parts)


def preflight(agent: str, path: Path | None = None, now: float | None = None, in_place: bool = False) -> str | None:
    """試行の中で更新が起きそうなら、止める理由（利用者への案内つき）。問題なければ None。in_place: 判定役が本物をその場で使う場合の文面。"""
    path = path or source_path(agent)
    if path is None:
        return None
    info = inspect(agent, path, now)
    if info.get("kind") != "oauth":
        return None
    why = None
    if info.get("remaining_s") is not None and info["remaining_s"] < MIN_REMAINING_S:
        why = "切れています" if info["remaining_s"] <= 0 else f"あと {info['remaining_s'] / 60:.0f} 分で切れます"
    elif info.get("refresh_age_s") is not None and info["refresh_age_s"] > MAX_REFRESH_AGE_S:
        why = f"最終更新から {info['refresh_age_s'] / 86400:.0f} 日たっています"
    if why is None:
        return None
    how = ("`codex login` するか、一度 `codex` を実行して更新してから" if agent == "codex"
           else "一度 `claude` を実行してログインを更新してから")
    key = "CODEX_API_KEY" if agent == "codex" else "ANTHROPIC_API_KEY"
    risk = ("判定の途中で本物がその場で更新されます（同時に別のプロセスが使うと refresh token が使用済みになり、使えなくなります）"
            if in_place else "このまま複製すると試行の中で更新が起き、本物の認証が使えなくなります（refresh token は使い回せないため）")
    return (f"{agent} の認証（{path.name}）が{why}。{risk}。{how}やり直してください。会社の評価では API キー（{key}）を inherit_env で"
            "渡す経路を勧めます")


@dataclass
class Lent:
    agent: str
    src: Path
    dst: Path
    digest: str


def _sha(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


class _FileLock:
    """プロセス内のロック + 本物の認証ファイルそのものへの flock（POSIX。新しいファイルは作らない）。"""

    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def __enter__(self) -> "_FileLock":
        _LOCK.acquire()
        if fcntl is not None and self.path.is_file():
            try:
                self.fh = open(self.path, "rb")
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                self.fh = None
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.fh is not None:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
                self.fh.close()
        finally:
            _LOCK.release()


def lend(agent: str, dst_dir: Path) -> Lent | None:
    """本物の認証ファイルを dst_dir に複製する（ロックの中で）。ファイルがなければ None。"""
    src = source_path(agent)
    if src is None or not src.is_file():
        return None
    dst = dst_dir / src.name
    with _FileLock(src):
        data = src.read_bytes()
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(dst, flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(dst, 0o600)
        except OSError:
            pass
    return Lent(agent, src, dst, hashlib.sha256(data).hexdigest())
