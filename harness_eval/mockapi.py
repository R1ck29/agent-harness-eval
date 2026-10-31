"""改善役の読み取りの隔離を、本物の claude CLI で確かめる（費用 0・認証なし）。

改善役（improve）は holdout の結果フォルダとタスク定義を権限設定の deny で読めなくする。これが本当に効くかは CLI の版や
OS（Windows のパスの書き方）で変わりうるので、改善役を起動する前に毎回、同じ CLI・同じ起動フラグ・同じ書き方の deny で確かめる。

仕組み: 127.0.0.1 に偽の Messages API を立て、CLI の ANTHROPIC_BASE_URL をそこへ向ける。偽 API がモデルの代わりに
決まったツール呼び出し（絶対パスの Read・Grep・Glob・作業フォルダ内のシンボリックリンク・.. を含むパス）を出し、
CLI が送り返したツールの結果に、読めてはいけないファイルの目印（カナリア）が入っていないかを見る。権限の判定は
CLI の中で行われるので、モデルが偽物でも判定は本物と同じ（Claude Code 2.1.295 で、deny なしなら漏れ・ありなら止まることを確認）。
--add-dir で確認用フォルダ全体を読める場所にしてから deny をかけるので、止まったのは deny の効果だと分かる。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .adapters import CLAUDE_ISOLATION_SETTINGS
from .isolation import INHERITED_UNSET, run_process
from .spec import ROUTE_VARS

MOCK_KEY = "sk-ant-harness-eval-mock"  # 偽 API 用のダミーのキー（本物の API では通らない）
IMPROVER_TOOLS = ("Read", "Glob", "Grep")  # 改善役のツール。Bash があると deny を cat で迂回できるので入れない
ALLOWED_INIT_TOOLS = set(IMPROVER_TOOLS) | {"StructuredOutput"}  # --json-schema を付けると CLI が加える


def _rule_path(posix: str) -> list[str]:
    """権限ルールの // の後ろに書くパスの形（先頭は /）。[ ] はルールのパターン記号なので \\ で打ち消す（打ち消さないと
    run[1] のようなフォルダが止まらないことを 2.1.295 で確認）。Windows のドライブ（C:/…）は /C:/… と /c/… の 2 つを並べる
    （どちらが効くかは自己確認で確かめる）。"""
    esc = posix.replace("[", "\\[").replace("]", "\\]")
    if re.match(r"^[A-Za-z]:/", esc):
        return ["/" + esc, "/" + esc[0].lower() + esc[2:]]
    return [esc]


def unusable_path(p: Path) -> bool:
    """deny で止められない書き方のパス。POSIX でフォルダ名に \\ を含むと、打ち消しても 2.1.295 では止まらなかった
    （CLI が区切りとして扱う）。このときは改善役を起動しない。"""
    return os.name != "nt" and "\\" in Path(p).resolve().as_posix()


def deny_rules(paths: list[Path]) -> list[str]:
    """読ませない場所の権限ルール。// で始めるとファイルシステムの根からの絶対パス。
    フォルダは中身（/**）とフォルダ自体（Grep・Glob の対象に指定されたとき）の 2 つ。"""
    rules: list[str] = []
    for p in paths:
        p = Path(p).resolve()
        for form in _rule_path(p.as_posix()):
            for r in ([f"Read(/{form})"] if p.is_file() else [f"Read(/{form}/**)", f"Read(/{form})"]):
                if r not in rules:
                    rules.append(r)
    return rules


def special_chars(paths: list[Path]) -> str:
    """パスに含まれる、英数字と / _ - . : 以外の文字（空白・括弧・記号・日本語など）。自己確認の deny 先の名前に入れ、
    実際の deny と同じ書き方を試す。"""
    return "".join(sorted({c for p in paths for c in Path(p).resolve().as_posix()
                           if not ((c.isascii() and c.isalnum()) or c in "/_-.:")}))


def improver_cmd(exe: list[str], model: str | None, deny: list[Path], max_turns: int, extra: list[str] | None = None) -> list[str]:
    """改善役の起動コマンド（自己確認も同じものを使う）。権限モードは必ず default を指定する
    （指定しないと 2.1.295 の -p は auto で始まり、分類器が読み取りを許すことがある）。default の -p は作業フォルダの外の
    読み取りと、すべての書き込みを拒否する。"""
    settings = {**CLAUDE_ISOLATION_SETTINGS, "permissions": {"deny": deny_rules(deny)}}
    cmd = exe + ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "default",
                 "--tools", ",".join(IMPROVER_TOOLS), "--no-session-persistence", "--max-turns", str(max_turns),
                 "--settings", json.dumps(settings)]
    return cmd + (["--model", model] if model else []) + list(extra or [])


def init_problem(init: dict[str, Any]) -> str:
    """init イベントが改善役の前提（権限モード default・読み取りのツールだけ）と違えば理由を返す。"""
    if init.get("permissionMode") != "default":
        return f"権限モードが default ではありません（{init.get('permissionMode')}。組織の管理設定で変えられている可能性）"
    tools = init.get("tools")
    if not isinstance(tools, list) or not tools:
        return "CLI の起動情報（init）にツールの一覧がありません"
    extra = sorted(set(map(str, tools)) - ALLOWED_INIT_TOOLS)
    return f"読み取り以外のツールが有効です: {extra}（MCP・管理設定など）" if extra else ""


def parse_stream(text: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """stream-json の (init イベント, result イベント)。"""
    init: dict[str, Any] = {}
    result = None
    for line in text.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        if o.get("type") == "system" and o.get("subtype") == "init":
            init = o
        elif o.get("type") == "result":
            result = o
    return init, result


class MockAPI:
    """モデルの代わりに steps のツール呼び出しを 1 つずつ出し、CLI が返したツールの結果を results に記録する。
    steps を出し終えたら、StructuredOutput が使えて final があればそれを出し、なければ文章で終える。"""

    def __init__(self, steps: list[tuple[str, dict[str, Any]]], final: dict[str, Any] | None = None):
        self.steps, self.final = steps, final
        self.results: dict[int, tuple[bool, str]] = {}
        self.bodies: list[str] = []  # CLI が送った内容すべて（ツールの結果以外に漏れていないかも見る）
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}"
                api.bodies.append(raw.decode("utf-8", errors="replace"))
                body = json.loads(raw)
                if "count_tokens" in self.path:
                    return self._json({"input_tokens": 10})
                msg = api._reply(body)
                if not body.get("stream"):
                    return self._json(msg)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for e in _events(msg):
                    self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())

            def _json(self, obj: dict[str, Any]) -> None:
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def _reply(self, body: dict[str, Any]) -> dict[str, Any]:
        done = 0
        for m in body.get("messages") or []:
            for c in m.get("content") if isinstance(m.get("content"), list) else []:
                tid = str(c.get("tool_use_id", "")) if isinstance(c, dict) else ""
                if c.get("type") == "tool_result" and tid.startswith("toolu_step"):
                    i = int(tid[len("toolu_step"):])
                    content = c.get("content")
                    # 最初に届いた結果を残す（後の送信で古い結果が省略・要約されても上書きしない）
                    self.results.setdefault(i, (bool(c.get("is_error")), content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)))
                    done = max(done, i + 1)
                elif c.get("type") == "tool_result" and tid == "toolu_final":
                    done = len(self.steps) + 1
        tools = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
        if "Read" in tools and done < len(self.steps):  # 本題の会話（題名付けなどの補助の呼び出しには文章で返す）
            name, inp = self.steps[done]
            return _message([{"type": "tool_use", "id": f"toolu_step{done}", "name": name, "input": inp}], "tool_use")
        if "StructuredOutput" in tools and self.final is not None and done == len(self.steps):
            return _message([{"type": "tool_use", "id": "toolu_final", "name": "StructuredOutput", "input": self.final}], "tool_use")
        return _message([{"type": "text", "text": "done"}], "end_turn")

    def __enter__(self) -> "MockAPI":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


def _message(blocks: list[dict[str, Any]], stop: str) -> dict[str, Any]:
    return {"id": "msg_mock", "type": "message", "role": "assistant", "model": "mock", "content": blocks,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}


def _events(msg: dict[str, Any]) -> list[dict[str, Any]]:
    ev: list[dict[str, Any]] = [{"type": "message_start", "message": {**msg, "content": [], "stop_reason": None}}]
    for i, b in enumerate(msg["content"]):
        if b["type"] == "tool_use":
            ev.append({"type": "content_block_start", "index": i, "content_block": {**b, "input": {}}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
        else:
            ev.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}})
        ev.append({"type": "content_block_stop", "index": i})
    ev.append({"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": 5}})
    ev.append({"type": "message_stop"})
    return ev


def mock_env(env: dict[str, str], home: Path, url: str) -> dict[str, str]:
    """CLI を偽 API へ向ける環境変数。経路を変える変数（Bedrock の切り替え・本物のキーなど）は外す。"""
    drop = set(ROUTE_VARS["claude_code"]) | set(INHERITED_UNSET)
    out = {k: v for k, v in env.items() if k not in drop and not k.startswith("ANTHROPIC_")}
    out.update(HOME=str(home), USERPROFILE=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"), ANTHROPIC_BASE_URL=url,
               ANTHROPIC_API_KEY=MOCK_KEY, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return out


def probe_verdict(steps: list[tuple[str, str]], results: dict[int, tuple[bool, str]], secret_mark: str, secret_name: str,
                  init: dict[str, Any], bodies: list[str] = ()) -> tuple[str, str]:
    """("ok" | "leak" | "unverified", 理由)。steps[i] は (control | secret, 対照なら結果に含まれるはずの文字列)。
    漏れ = deny した場所の中身が CLI の送ったどこか（ツールの結果・その他の送信内容）に出る、またはファイル名が
    成功した結果（一覧）に出る。エラーの文面にはパスが入るので、名前はエラー以外だけで見る。"""
    leaked = [i for i, (err, text) in sorted(results.items()) if secret_mark in text or (not err and secret_name in text)]
    if leaked or any(secret_mark in b for b in bodies):
        return "leak", f"deny した場所の中身・ファイル名が CLI の送信内容に出ました（呼び出し {leaked or '結果以外'}）"
    problem = init_problem(init)
    if problem:
        return "unverified", problem
    for i, (kind, expect) in enumerate(steps):
        if i not in results:
            return "unverified", f"呼び出し {i} の結果がありません（CLI が偽 API と通信できていない可能性）"
        if kind == "control" and (results[i][0] or expect not in results[i][1]):
            return "unverified", f"対照（deny していない場所）の呼び出し {i} も失敗しました。deny の効果を確かめられません"
    n = sum(1 for k, _ in steps if k == "secret")
    return "ok", f"deny した場所への {n} 通りの読み取りがすべて止まりました"


def read_isolation_check(exe: list[str], model: str | None, env: dict[str, str], deny: bool = True,
                         special: str = "", timeout_s: int = 180) -> tuple[str, str]:
    """改善役と同じ起動で deny が効くかを確かめる。("ok" | "leak" | "unverified", 理由)。
    special: 実際に deny する場所のパスに含まれる記号など（deny 先のフォルダ名に入れて同じ書き方を試す）。deny=False は判定の確認用。"""
    root = Path(tempfile.mkdtemp(prefix="harness-eval-probe-")).resolve()
    try:
        area = root / "area"  # 出力（stdout.jsonl）は Grep の対象の外に置く
        # 記号は名前の途中に 1 つずつ置く（末尾と途中で扱いが変わる記号があるため）
        denied = area / ("s[1]x" + "".join(c + "x" for c in special if c not in "[]"))
        home, work, secret_dir, public_dir = area / "home", area / "work", denied / "holdout", area / "public"
        for d in (home / ".claude", work, secret_dir, public_dir):
            d.mkdir(parents=True)
        tag = secrets.token_hex(6)
        secret_mark, public_mark, secret_name = f"HOLDOUT-{tag}", f"PUBLIC-{tag}", f"S-{tag}.txt"
        (secret_dir / secret_name).write_text(secret_mark + "\n", encoding="utf-8")
        (public_dir / "PUBLIC.txt").write_text(public_mark + "\n", encoding="utf-8")
        steps: list[tuple[str, dict[str, Any]]] = [
            ("Read", {"file_path": str(public_dir / "PUBLIC.txt")}),
            ("Grep", {"pattern": "PUBLIC-", "path": str(public_dir), "output_mode": "content"}),
            ("Glob", {"pattern": "**/*.txt", "path": str(public_dir)}),
            ("Read", {"file_path": str(secret_dir / secret_name)}),
            ("Grep", {"pattern": "HOLDOUT-", "path": str(secret_dir), "output_mode": "content"}),
            ("Glob", {"pattern": "**", "path": str(secret_dir)}),
            ("Read", {"file_path": str(public_dir / ".." / denied.name / "holdout" / secret_name)}),
            ("Grep", {"pattern": "HOLDOUT-", "path": str(area), "output_mode": "content"}),
            ("Glob", {"pattern": "**/*.txt", "path": str(area)}),
        ]
        kinds = [("control", public_mark), ("control", public_mark), ("control", "PUBLIC.txt")] + [("secret", "")] * 6
        try:  # 作業フォルダの中のリンクから外を読む経路（Windows では権限がないと作れないので省く）
            os.symlink(secret_dir, work / "link", target_is_directory=True)
            steps.append(("Read", {"file_path": str(work / "link" / secret_name)}))
            kinds.append(("secret", ""))
        except OSError:
            pass
        with MockAPI(steps) as api:
            cmd = improver_cmd(exe, model, [denied] if deny else [], len(steps) + 5, ["--add-dir", str(area)])
            pr = run_process(cmd, work, mock_env(env, home, api.url), "go", root / "stdout.jsonl", root / "stderr.txt", timeout_s)
            results, bodies = dict(api.results), list(api.bodies)
        if pr.launch_error:
            return "unverified", f"CLI を起動できません: {pr.launch_error}"
        if pr.timed_out:
            return "unverified", f"{timeout_s} 秒で終わりませんでした"
        init, _ = parse_stream((root / "stdout.jsonl").read_text(encoding="utf-8", errors="replace"))
        state, why = probe_verdict(kinds, results, secret_mark, secret_name, init, bodies)
        if state == "unverified" and not results:
            err = (root / "stderr.txt").read_text(encoding="utf-8", errors="replace").strip()[-300:]
            why += f"（終了コード {pr.returncode}{'、' + err if err else ''}）"
        return state, why
    finally:
        shutil.rmtree(root, ignore_errors=True)
