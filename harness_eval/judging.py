"""判定役（LLM judge）の段階・モデルの強さ・系統の決め方と、判定入力の盲検化。

段階（tier）:
  decisive … 採用・マージ判断用（既定）。強いモデル（Claude: opus／Codex: gpt-6.2-astra）、多数決（既定 3 回）、
             評価対象と別系統。両系統の CLI があれば、PASS には両系統の多数決の一致を要求する
  dev      … 安い開発用。中位モデル（Claude: sonnet／Codex: gpt-6.2-sol）で 1 回。結論は「暫定」で、採用判断には使えない

系統（family）: claude_code と codex。評価対象と同じ系統の判定役は、自分の系統の出力を甘く見る可能性があるため避ける。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

FAMILIES = ("claude_code", "codex")
TIERS = ("dev", "decisive")
TIER_MODELS = {
    "decisive": {"claude_code": "opus", "codex": "gpt-6.2-astra"},
    "dev": {"claude_code": "sonnet", "codex": "gpt-6.2-sol"},
}
TIER_VOTES = {"decisive": 3, "dev": 1}
# モデルの強さ。ここにないモデル（haiku・mini・不明）は弱い判定役として扱う（明示の上書き --allow-weak-judge が要る）
STRONG_MODELS = ("opus", "gpt-6.2-astra")
MID_MODELS = ("sonnet", "gpt-6.2-sol")
JUDGE_INPUT_VERSION = "2"  # 判定入力（プロンプト・盲検化）の作りを変えたら上げる（判定キャッシュを分けるため）


def model_strength(model: str | None) -> str:
    """strong / mid / weak。CLI の既定（None）は分からないので weak。claude-opus-5-5 のような完全な id も見る。"""
    m = (model or "").lower()
    if any(s in m for s in STRONG_MODELS):
        return "strong"
    if any(s in m for s in MID_MODELS):
        return "mid"
    return "weak"


def required_strength(tier: str) -> str:
    return "strong" if tier == "decisive" else "mid"


def strength_ok(model: str | None, tier: str) -> bool:
    order = {"weak": 0, "mid": 1, "strong": 2}
    return order[model_strength(model)] >= order[required_strength(tier)]


def other_family(agent: str) -> str:
    return "codex" if agent == "claude_code" else "claude_code"


def plan_families(evaluated: Iterable[str], available: Iterable[str], tier: str, forced: str | None = None) -> dict[str, Any]:
    """評価対象のエージェントごとに、判定に使う系統（先頭が主、2 つ目は一致を確かめる系統）を決める。

    - forced（--judge-agent）があればその系統だけ（評価対象と同じなら same_family）
    - 既定: 主は評価対象の別系統。decisive で両系統が使えるなら、評価対象と同じ系統を足す（両系統一致）
    - 別系統の CLI がなければ同系統で動かし same_family を立てる（結論に LLM 判定が決定的に効くなら INCONCLUSIVE になる）"""
    avail = [f for f in FAMILIES if f in set(available)]
    out: dict[str, Any] = {}
    for agent in sorted(set(evaluated)):
        if forced:
            fams = [forced]
        else:
            cross = other_family(agent)
            if cross in avail:
                fams = [cross] + ([agent] if tier == "decisive" and agent in avail else [])
            elif agent in avail:
                fams = [agent]
            else:
                fams = []
        out[agent] = {"families": fams, "same_family": bool(fams) and all(f == agent for f in fams),
                      "both_families": len(set(fams)) == 2}
    return out


MASK = "[MASKED]"
# ハーネス・ベンダー固有の痕跡（どの条件・系統の出力かを判定役に教えうる語）
_VENDOR_TERMS = (r"claude[ -]?code", r"\bclaude\b", r"\banthropic\b", r"\bcodex\b", r"\bopenai\b", r"\bchatgpt\b",
                 r"\bgpt-[\w.-]+", r"\bopus\b", r"\bsonnet\b", r"\bhaiku\b", r"CLAUDE\.md", r"AGENTS\.md",
                 r"\.claude\b", r"\.codex\b", r"\.agents\b", r"SKILL\.md")
_TEMP_PATH = re.compile(r"(?:/tmp/|/var/folders/|/private/var/|[A-Za-z]:\\Users\\)[^\s'\"`)\]]*")


def blind(text: str, terms: Iterable[str] = (), paths: Iterable[str] = ()) -> str:
    """判定入力から、どの条件・プロファイル・ハーネス・モデルの出力かが分かる痕跡を伏せる。
    terms: プロファイル id・モデル名・配布物のスキル名など（大文字小文字を区別せず、語として一致するものを伏せる）。
    paths: 試行の作業フォルダ・一時ホームなどの絶対パス（そのまま伏せる。長い順に置き換える）。"""
    out = text
    for p in sorted({p for p in paths if p and len(p) > 1}, key=len, reverse=True):
        out = out.replace(p, "<PATH>")
    out = _TEMP_PATH.sub("<PATH>", out)
    for t in sorted({t for t in terms if t and len(t) >= 3}, key=len, reverse=True):
        out = re.sub(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])", MASK, out, flags=re.IGNORECASE)
    for pat in _VENDOR_TERMS:
        out = re.sub(pat, MASK, out, flags=re.IGNORECASE)
    return out


def judge_cache_key(prompt: str, family: str, model: str | None, rubric: str, tier: str, vote: int) -> str:
    """判定キャッシュのキー: 判定入力（盲検化後のプロンプト）+ 判定モデル + ルーブリック + 段階 + 何票目か。"""
    raw = "\x1f".join([JUDGE_INPUT_VERSION, family, str(model), tier, str(vote),
                       hashlib.sha256(rubric.encode()).hexdigest(), hashlib.sha256(prompt.encode()).hexdigest()])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def majority(votes: list[str]) -> str:
    """pass / fail / unknown の多数決（過半数がなければ unknown）。"""
    for v in ("pass", "fail", "unknown"):
        if votes.count(v) * 2 > len(votes):
            return v
    return "unknown"
