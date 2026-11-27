"""グレーダー（採点器）。

status:
  pass / fail          … 通常の判定
  unknown              … LLM 判定が「根拠不足」と答えた（不合格扱い）
  not_run              … --no-llm で LLM 判定を実行しなかった（スコアの分母から除外）
  skipped_prior_fail   … コード判定が既に不合格なので LLM 判定を省いた（不合格扱い。
                          試行の合否は変わらないのでトークン節約のため省略する）
  pending              … 判定待ち（runner が全試行のあとにまとめて判定する。結果には残らない）
  judge_disagreement   … 両系統の判定役の多数決が食い違った（合格として数えない）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .isolation import fill, grader_env, grader_isolation, grader_sandbox_cmd, open_new
from .record import TrialRecord
from .spec import Grader

JUDGE_PROMPT = """あなたは厳格な採点者です。以下の「評価観点」だけに基づき、「対象」を判定してください。
外部知識で補わず、対象に書かれていることだけで判断します。根拠が不足していれば UNKNOWN とします。

# 評価観点
{criteria}

# 対象（{focus}）
<<<
{content}
>>>

1 行目に PASS / FAIL / UNKNOWN のいずれか 1 語だけを書き、2 行目に理由を 1 文で書いてください。"""


@dataclass
class JudgeConfig:
    enabled: bool = True
    run: Callable[[str], tuple[str | None, float | None]] | None = None  # prompt -> (text, cost)
    # True: その場では判定せず、判定入力（最小: 評価観点と最終応答／対象ファイルだけ。会話の記録は渡さない）を
    # rec.judge_inputs に積んで status を pending にする。runner が全試行のあとに盲検化・順序のランダム化・多数決で判定する
    defer: bool = False


def judge_content(focus: str, max_chars: int, rec: TrialRecord, workdir: Path) -> str | None:
    """判定に渡す対象（最終応答か、作業フォルダの 1 ファイル）。ファイルがなければ None。"""
    if focus == "final":
        content = rec.final_message or ""
    else:
        f = _safe_path(workdir, focus)
        if not f.is_file():
            return None
        content = f.read_text(encoding="utf-8", errors="replace")
    return content[:max_chars] + "\n…(以下省略)" if len(content) > max_chars else content


def parse_verdict(text: str | None) -> tuple[str, str]:
    """判定役の応答の 1 行目から pass / fail / unknown を読む。"""
    if not text:
        return "unknown", "判定モデルの応答なし"
    first = text.strip().splitlines()[0].strip().upper().strip("*`# ")
    reason = " ".join(text.strip().splitlines()[1:2])[:200]
    if first.startswith("PASS"):
        return "pass", reason
    if first.startswith("FAIL"):
        return "fail", reason
    return "unknown", reason or text[:200]


def _safe_path(workdir: Path, rel: str) -> Path:
    p = (workdir / rel).resolve()
    if workdir.resolve() not in p.parents and p != workdir.resolve():
        raise ValueError(f"作業ディレクトリ外のパス: {rel}")
    return p


# 確認の質問の判定:
# 1. '?' / '？' を含む（現状どおり）
# 2. 文に 'ますでしょうか' / 'でしょうか' / 'いただけますか' / '頂けますか' / 'ましょうか' / 'ますか' / 'ですか' を含む
#    （'ますから' / 'でしょうから' / 'ですから' のような理由の表現は除く。相づちの「そうですか」は除く）
# 3. 指定・選択・回答・教示を求める語に続く 'ください' / '下さい'（作業の復唱だけの「保存してください」は通さない。
#    相手に確かめさせる「ご確認ください」は数えない）
# 2・3 とも、次のものは数えない（推測で作業を済ませたあとの締めの言葉）:
#    - 同じ文で前に条件の節（〜があれば / 〜なら / 〜でしたら / 〜場合は）がある
#    - その文か、それより前の文で作業の完了（抽出しました / 保存済み など。下調べの「確認しました」は除く）を述べた
#    - 締めの言い回し（「いつでも」「どうぞ」「他に何かありますか」「いかがでしょうか」「これでよろしいでしょうか」）
_CLARIFY_QUESTION_RE = re.compile(
    r"(?:ますでしょうか|でしょうか|いただけますか|頂けますか|ましょうか|ますか|ですか)(?=[。、）)（(」』\s]|$)"
)
_CLARIFY_REQUEST_RE = re.compile(
    r"(?:ご指定|指定して|ご教示|教示して|ご指示|指示して|お聞かせ|聞かせて|"
    r"お選び|選んで|ご選択|選択して|ご回答|回答して|お答え|答えて|"
    r"ご共有|共有して|ご提示|提示して|ご入力|入力して|ご返信|返信して|"
    r"お伝え|伝えて|明確にして|教えて|お教え|確認させて|お知らせ|知らせて)(?:ください|下さい)"
)
_TASK_CONDITION_RE = re.compile(r"条件|対象|期間|範囲|詳細|項目|形式|希望|意図|前提|要件|仕様|保存先|出力先")
_CONDITIONAL_RE = re.compile(r"あれば|ありましたら|でしたら|ようなら|場合は|場合には|際は|際には|"
                            r"(?<!よろし)(?<!差し支えな)(?<!差支えな)ければ|たら|なら(?![ずびない])")  # 「よろしければ」は前置きの言葉
# 「〜たらいいですか」「〜してよいですか」のような、条件の形をした確認の質問は条件節に数えない
_CONDITIONAL_OK_RE = re.compile(r"(?:て(?:も)?)?(?:よい|いい|良い|よろしい)(?:か|です|でしょう)")
_CONDITIONAL_OK_TOKENS = {"たら", "なら", "ければ", "でしたら"}
# 作業そのものの完了（下調べの「確認しました」「読み込みました」や、受け答えの「了解しました」は含めない）
_DONE_RE = re.compile(r"(?:抽出|保存|出力|作成|書き出|書き込|集計|計算|生成|処理|実行|完了|更新|修正|変換|整形|まとめ)"
                      r"(?:し|を行い|いたし)?(?:ました|済み|済です)(?!か|でしょうか|(?<=済み)です(?:か|でしょうか))|作業(?:を|が)?(?:終え|終わり|完了)(?!しましたか|ましたか|ますか|(?:です|でしょう)?か)")
# 下調べの完了報告（作業そのものの完了ではない）。これだけは「完了」扱いにしない
_PREP_DONE_RE = re.compile(r"(?:確認|読み込み|読込)(?:が|を)?完了(?:し|いたし)ました|現状を(?:簡単に)?まとめ(?:ました|いたしました)")
_SIGN_OFF_RE = re.compile(r"お手伝い|お役に立て|いかが|いつでも|どうぞ|お気軽|遠慮なく|何なり|"
                          r"^\s*(?:これ|こちら|以上|この内容)で|^\s*(?:そう|そうなん|本当|ほんと)ですか|"
                          r"^\s*(?:他|ほか|その他|追加)(?:に|で)")
_SENTENCE_SPLIT_RE = re.compile(r"[。．！!\n]+")



def _blocking_conditional(sentence: str, end: int) -> bool:
    """条件の節があるか。ただし「〜たらいいですか」「〜してよいですか」は確認の質問なので数えない。"""
    for m in _CONDITIONAL_RE.finditer(sentence, 0, end):
        # 質問の終わり（ですか 等）も含めて見る。before だけだと「たらいいですか」の「ですか」が落ちる
        if m.group(0) in _CONDITIONAL_OK_TOKENS and _CONDITIONAL_OK_RE.search(sentence[m.end():]):
            continue
        return True
    return False


def _clarifies(sentence: str) -> bool:
    """1 文が作業前の確認になっているか（? / ？ を含まない文。前の文での作業の完了は呼び出し側が見る）。"""
    def fresh(m: re.Match[str]) -> bool:  # 同じ文の前に条件の節・完了の表現がない
        before = sentence[:m.start()]
        return not _blocking_conditional(sentence, m.start()) and _DONE_RE.search(before) is None

    if _SIGN_OFF_RE.search(sentence) and not _TASK_CONDITION_RE.search(sentence):
        return False
    return any(fresh(m) for rx in (_CLARIFY_QUESTION_RE, _CLARIFY_REQUEST_RE) for m in rx.finditer(sentence))


_URL_RE = re.compile(
    r"https?://[A-Za-z0-9._~:/#\[\]@!$&'()*+,;=%-]*(?:\?[A-Za-z0-9._~:/#\[\]@!$&'()*+,;=%-]+)?",
    re.I,
)


def is_clarification_question(text: str) -> bool:
    """最終応答が、作業前に条件を確かめる質問・確認依頼になっているか。
    URL のクエリ（?q=）は質問記号として数えない。"""
    if not text or not text.strip():
        return False
    stripped = _PREP_DONE_RE.sub(" ", _URL_RE.sub(" ", text))
    if _DONE_RE.search(stripped):
        return False  # 質問があっても、作業を済ませたと述べる文が（下調べの報告を除いて）あれば確認ではない
    if "?" in stripped or "？" in stripped:
        return True
    for s in _SENTENCE_SPLIT_RE.split(stripped):
        if _clarifies(s):
            return True
    return False


def _count_check(n: int, params: dict[str, Any]) -> tuple[bool, str]:
    lo = params.get("min", 1 if "max" not in params else 0)
    hi = params.get("max")
    ok = n >= lo and (hi is None or n <= hi)
    rng = f">= {lo}" + (f" かつ <= {hi}" if hi is not None else "")
    return ok, f"{n} 回（期待: {rng}）"


_CMD_KEYS = ("command", "cmd", "script")


def tool_input_haystack(name: str, inp: Any) -> str:
    """input_match の検索対象。Bash 系はコマンド文字列を先に置き、複数行モードで行頭の ^ が効くようにする。
    続けて他の文字列フィールドと json.dumps も置く（既存の JSON 向けパターンも動く）。"""
    parts: list[str] = []
    if isinstance(inp, dict):
        for k in _CMD_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
            elif isinstance(v, (list, tuple)):
                parts.append(" ".join(str(x) for x in v))
        # コマンド以外の文字列は行頭に置かない（description が ^ に誤一致するのを防ぐ）
    elif isinstance(inp, str):
        parts.append(inp)
    try:
        parts.append(json.dumps(inp, ensure_ascii=False) if inp is not None else "")
    except (TypeError, ValueError):
        parts.append(str(inp))
    return "\n".join(parts)


def grade_one(g: Grader, rec: TrialRecord, workdir: Path, task_dir: Path | None = None,
              tmp_dir: Path | None = None) -> tuple[bool, str]:
    t, p = g.type, g.params
    if t == "file_exists":
        f = _safe_path(workdir, p["path"])
        return f.is_file(), f"{p['path']} {'あり' if f.is_file() else 'なし'}"
    if t == "file_contains":
        f = _safe_path(workdir, p["path"])
        if not f.is_file():
            return False, f"{p['path']} がありません"
        text = f.read_text(encoding="utf-8", errors="replace")
        hit = re.search(p["pattern"], text, re.MULTILINE) is not None
        return hit, f"/{p['pattern']}/ {'一致' if hit else '不一致'}"
    if t == "regex":
        text = rec.final_message or ""
        hit = re.search(p["pattern"], text, re.MULTILINE) is not None
        return hit, f"最終応答が /{p['pattern']}/ に{'一致' if hit else '不一致'}"
    if t == "clarification":
        hit = is_clarification_question(rec.final_message or "")
        return hit, f"最終応答が確認の質問{'になっている' if hit else 'になっていない'}"
    if t == "tool_used":
        name_re = re.compile(p["tool"])
        inp_re = re.compile(p["input_match"], re.MULTILINE) if p.get("input_match") else None
        n = 0
        for c in rec.tool_calls:
            if not p.get("include_subagents", True) and c.get("from_subagent"):
                continue
            if not name_re.fullmatch(c["name"]):
                continue
            if inp_re and not inp_re.search(tool_input_haystack(c.get("name", ""), c.get("input"))):
                continue
            n += 1
        return _count_check(n, p)
    if t == "skill_used":
        n = sum(1 for s in rec.skill_calls if s == p["skill"] or s.endswith(":" + p["skill"]))
        ok, why = _count_check(n, p)
        return ok, why + ("（推定）" if rec.skill_detection == "heuristic" else "")
    if t == "subagent_used":
        n = sum(1 for s in rec.subagent_calls if "name" not in p or s == p["name"] or s.endswith(":" + str(p["name"])))
        return _count_check(n, p)
    if t == "hook_used":
        rx = re.compile(str(p["hook"]))
        n = sum(1 for h in rec.hook_calls or [] if rx.fullmatch(str(h.get("name")))
                and ("outcome" not in p or h.get("outcome") == p["outcome"]))
        return _count_check(n, p)
    if t == "command":
        # 置換した値はシェル用に引用される（空白を含むパスでも壊れない）。YAML 側では引用符で囲まないこと
        ph = {"python": sys.executable, "workdir": str(workdir)}
        if task_dir is not None:
            ph["task_dir"] = str(task_dir)
        cmd = fill(str(p["run"]), ph, quote=True)
        mode = grader_isolation()
        if rec.meta.get("grader_isolation") != "none":
            rec.meta["grader_isolation"] = mode
        with tempfile.TemporaryDirectory(prefix="ahe-grade-", dir=tmp_dir) as d:
            gtmp = Path(d)
            rec_path = gtmp / "record_for_grader.json"  # 試行ルートではなく採点の一時フォルダ（solver から見えない・採点のあとに消える）
            with open_new(rec_path, "w") as fh:
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False))
            env = grader_env(workdir, gtmp, rec_path)
            try:
                if mode == "bwrap":
                    r = subprocess.run(grader_sandbox_cmd(cmd, workdir, gtmp, [fill(x, ph) for x in p.get("ro", [])]), cwd=workdir, env=env,
                                       capture_output=True, text=True, timeout=int(p.get("timeout_s", 120)), stdin=subprocess.DEVNULL)
                else:
                    r = subprocess.run(cmd, shell=True, cwd=workdir, env=env, capture_output=True, text=True,
                                       timeout=int(p.get("timeout_s", 120)), stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return False, "採点コマンドがタイムアウト"
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
        return r.returncode == 0, f"exit={r.returncode} {tail[0][:200]}"
    raise ValueError(t)


def _judge(g: Grader, rec: TrialRecord, workdir: Path, judge: JudgeConfig) -> tuple[str, str, float | None]:
    focus = g.params.get("focus", "final")
    content = judge_content(focus, int(g.params.get("max_chars", 20000)), rec, workdir)
    if content is None:
        return "fail", f"{focus} がありません", 0.0  # 判定は呼んでいない
    assert judge.run is not None
    text, cost = judge.run(JUDGE_PROMPT.format(criteria=g.params["criteria"], focus=focus, content=content))
    st, reason = parse_verdict(text)
    return st, reason, cost


class JudgeUnavailable(Exception):
    """判定モデルを呼べなかった（認証・起動・タイムアウトなど）。試行は集計から除外される。"""


def blocks_score(g: dict[str, Any] | None) -> bool:
    """合否・スコア・verdict に使う判定か（weight 0 / report_only は使わない）。"""
    return bool(g) and not g.get("report_only") and float(g.get("weight", 1) or 0) > 0


def grade_all(graders: list[Grader], rec: TrialRecord, workdir: Path, judge: JudgeConfig,
              task_dir: Path | None = None, tmp_dir: Path | None = None, precheck: dict[str, Any] | None = None) -> None:
    """rec.grader_results / score / judge_cost_usd を埋める。
    precheck: 採点の前の検査の結果（見えているテストの改ざんなど）。不合格の 1 行として末尾に足し、LLM 判定は走らせない。"""
    results: list[dict[str, Any]] = []
    for g in graders:
        if g.type == "llm":
            results.append(None)  # type: ignore[arg-type]
            continue
        base = {"label": g.label(), "type": g.type, "kind": g.kind, "weight": g.weight, "category": g.category,
                "harness_only": g.requires_harness, "report_only": g.report_only or g.weight == 0}
        if g.type == "hook_used" and rec.hook_calls is None:  # Codex の --json には hooks の実行記録が出ない（0.163.0 で実測）
            results.append({**base, "status": "not_run", "reason": "hooks の実行記録がないため判定できません（Codex）"})
            continue
        try:
            ok, why = grade_one(g, rec, workdir, task_dir, tmp_dir)
            if g.negate:
                ok = not ok
            status = "pass" if ok else "fail"
        except Exception as e:  # 採点器の不具合は不合格として理由を残す
            status, why = "fail", f"採点エラー: {e}"
        results.append({**base, "status": status, "reason": why})
    # LLM 判定を省くのは「配布物の有無に関係なく既に不合格」のときだけ。harness_only の不合格（基準側では当然）で
    # 省くと、基準との比較用スコアが出せなくなる
    if precheck is not None:  # 判定の番号（graders の位置）をずらさないよう末尾に足す
        results.append(precheck)
    def _blocks(r: dict[str, Any] | None) -> bool:
        return blocks_score(r) and not r["harness_only"]

    code_ok = all(r is None or not _blocks(r) or r["status"] in ("pass", "not_run") for r in results) and rec.error is None
    jcost = 0.0
    jcost_known = True
    for i, g in enumerate(graders):
        if g.type != "llm":
            continue
        base = {"label": g.label(), "type": g.type, "kind": g.kind, "weight": g.weight, "category": g.category,
                "harness_only": False, "report_only": g.report_only or g.weight == 0}
        if not judge.enabled or (judge.run is None and not judge.defer):
            results[i] = {**base, "status": "not_run", "reason": "--no-llm"}
        elif not code_ok:
            results[i] = {**base, "status": "skipped_prior_fail", "reason": "他の判定が不合格のため省略"}
        elif judge.defer:
            focus = g.params.get("focus", "final")
            content = judge_content(focus, int(g.params.get("max_chars", 20000)), rec, workdir)
            if content is None:
                results[i] = {**base, "status": "fail", "reason": f"{focus} がありません"}
            else:
                rec.judge_inputs.append({"kind": "rubric", "grader": i, "criteria": str(g.params["criteria"]), "focus": focus,
                                         "content": content, "negate": g.negate})
                results[i] = {**base, "status": "pending", "reason": "判定待ち"}
        else:
            try:
                st, why, cost = _judge(g, rec, workdir, judge)
            except JudgeUnavailable:
                raise
            except Exception as e:
                st, why, cost = "unknown", f"判定エラー: {e}", 0.0  # 判定呼び出し前後の不具合（呼び出し失敗は JudgeUnavailable）
            if g.negate and st in ("pass", "fail"):
                st = "fail" if st == "pass" else "pass"
            if cost is None:
                jcost_known = False
            else:
                jcost += cost
            results[i] = {**base, "status": st, "reason": why}
    rec.grader_results = results
    rescore(rec)
    # 判定を実行しなければ 0、実行して費用が分からなければ None（集計で「不明」として扱う）
    rec.judge_cost_usd = jcost if jcost_known else None


def rescore(rec: TrialRecord) -> None:
    """grader_results から score / cmp_score を計算する。実行しなかった判定（not_run・skipped_prior_fail）は分母に入れない。
    判定待ち（pending）があるうちは None（まだ採点が終わっていない）。"""
    results = rec.grader_results
    if any(r["status"] == "pending" for r in results):
        rec.score = rec.cmp_score = None
        return
    scored = [r for r in results if r["status"] not in ("not_run", "skipped_prior_fail")]
    rec.score = _weighted(scored)
    rec.cmp_score = _weighted([r for r in scored if not r["harness_only"]])


def _weighted(results: list[dict[str, Any]]) -> float | None:
    """合格した判定の重み付き割合。weight 0 / report_only は分母に入れない（合否に使わない）。
    判定が 1 つもなければ None。報告専用だけなら 1.0（止める判定がない）。"""
    blocking = [r for r in results if blocks_score(r)]
    if not blocking:
        return None
    total = sum(r["weight"] for r in blocking)
    return sum(r["weight"] for r in blocking if r["status"] == "pass") / total
