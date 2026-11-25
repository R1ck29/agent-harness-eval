"""判定役（LLM judge）の段階・モデルの強さ・系統の決め方と、判定入力の盲検化。

段階（tier）:
  decisive … 採用・マージ判断用。強いモデル（Claude: opus／Codex: gpt-6.2-astra）、多数決（既定 3 回）、
             評価対象と別系統。両系統の CLI があれば、PASS には両系統の多数決の一致を要求する
  dev      … 安い開発用（既定）。中位モデル（Claude: sonnet／Codex: gpt-6.2-sol）で 1 回。LLM 判定が合否を決めた結論は「暫定」で、採用判断には使えない

系統（family）: claude_code と codex。評価対象と同じ系統の判定役は、自分の系統の出力を甘く見る可能性があるため避ける。
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Iterable

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
JUDGE_INPUT_VERSION = "3"  # 判定入力（プロンプト・盲検化）の作りを変えたら上げる（判定キャッシュを分けるため）


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


def judge_identity(exe: list[str], version: str | None, model: str | None, route: str = "", path: str | None = None) -> str:
    """判定役の識別子: PATH で見つけた実行ファイルと引数のファイルの実体・内容、CLI の版、実効モデル、非秘密の経路設定。"""
    parts = []
    for i, a in enumerate(exe):
        file = (shutil.which(a, path=path) or a) if i == 0 else a
        if not os.path.isfile(file):
            parts.append(a)
            continue
        real = os.path.realpath(file)
        digest = hashlib.sha256()
        with open(real, "rb") as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
        parts.append(f"{os.path.abspath(file)}:{real}:{digest.hexdigest()}")
    raw = "\x1f".join(["|".join(parts), str(version), str(model) if model else "(CLI の既定)", route])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def judge_cache_key(prompt: str, family: str, model: str | None, rubric: str, tier: str, vote: int, identity: str = "") -> str:
    """判定キャッシュのキー: 判定入力（盲検化後のプロンプト）+ 判定モデル + ルーブリック + 段階 + 何票目か + 判定役の識別子。"""
    raw = "\x1f".join([JUDGE_INPUT_VERSION, family, str(model), tier, str(vote), identity,
                       hashlib.sha256(rubric.encode()).hexdigest(), hashlib.sha256(prompt.encode()).hexdigest()])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def majority(votes: list[str]) -> str:
    """pass / fail / unknown の多数決（過半数がなければ unknown）。"""
    for v in ("pass", "fail", "unknown"):
        if votes.count(v) * 2 > len(votes):
            return v
    return "unknown"


def infer_family(model: str) -> str:
    """モデル名から系統を推定する（--judge-model に系統を書かなかったとき）。gpt-* は codex、それ以外は claude_code。
    プロバイダの前置き（openai/）やタグ（:free）を含んでもよい: / と : で区切ったどこかが gpt・o1・o3・o4 で始まれば codex。"""
    parts = re.split(r"[/:]", model.strip().lower())
    return "codex" if any(p.startswith(("gpt", "o1", "o3", "o4")) for p in parts) else "claude_code"


@dataclass(frozen=True)
class JudgeSettings:
    """この実行の判定役の設定（実行前に決め、summary.json の meta.judge と事前登録に残す）。"""
    tier: str
    models: dict[str, str | None]
    votes: int
    seed: int
    plan: dict[str, Any]
    available: tuple[str, ...]
    forced: str | None = None
    weak_allowed: bool = False
    calibration_items: int = 6

    def families_for(self, agent: str) -> list[str]:
        return list((self.plan.get(agent) or {}).get("families") or [])

    def used_families(self) -> list[str]:
        return sorted({f for p in self.plan.values() for f in p["families"]})

    def weak(self) -> list[str]:
        return [f"{f}: {self.models.get(f) or 'CLI の既定'}" for f in self.used_families() if not strength_ok(self.models.get(f), self.tier)]

    def meta(self, calibration: dict[str, Any] | None = None, usage: dict[str, Any] | None = None,
             actual_models: dict[str, Any] | None = None) -> dict[str, Any]:
        used = self.used_families()
        out: dict[str, Any] = {"tier": self.tier, "llm": True, "models": {f: self.models.get(f) for f in used},
                               "strength": {f: model_strength(self.models.get(f)) for f in used}, "weak_allowed": self.weak_allowed,
                               "weak": self.weak(), "blind": True, "votes": self.votes, "seed": self.seed, "order": "seed で並べ替えた順",
                               "plan": self.plan, "available": list(self.available), "forced": self.forced,
                               "input": "評価観点 + 最終応答か対象ファイル 1 つ（盲検化済み。会話の記録・ツールの入出力は渡さない）",
                               "calibration": calibration, "usage": usage}
        if actual_models:
            out["actual_models"] = dict(actual_models)
        return out


def make_settings(tier: str, evaluated: Iterable[str], available: Iterable[str], forced: str | None = None,
                  model_overrides: dict[str, str] | None = None, votes: int | None = None, allow_weak: bool = False,
                  seed: int = 0, calibration_items: int = 6) -> JudgeSettings:
    """段階の既定（モデル・回数・系統）に上書きを当てる。弱い判定役（段階が求める強さ未満）は allow_weak がなければ止める。"""
    from .spec import SpecError
    if tier not in TIERS:
        raise SpecError(f"--tier は {' / '.join(TIERS)}")
    if votes is not None and votes < 1:
        raise SpecError("--judge-votes は 1 以上")
    if calibration_items < 1:
        raise SpecError("--calibration-items は 1 以上")
    models: dict[str, str | None] = dict(TIER_MODELS[tier])
    models.update(model_overrides or {})
    plan = plan_families(evaluated, available, tier, forced)
    s = JudgeSettings(tier=tier, models=models, votes=votes or TIER_VOTES[tier], seed=seed, plan=plan,
                      available=tuple(f for f in FAMILIES if f in set(available)), forced=forced, weak_allowed=allow_weak,
                      calibration_items=calibration_items)
    if s.weak() and not allow_weak:
        raise SpecError(f"判定役のモデルが {tier} 段階には弱すぎます（{', '.join(s.weak())}。{tier} は "
                        f"{'opus / gpt-6.2-astra' if tier == 'decisive' else 'sonnet / gpt-6.2-sol 以上'}）。"
                        "承知の上で使うなら --allow-weak-judge（レポートに「弱い判定役」と出て、結論は採用判断に使えません）")
    return s


def combine(majorities: dict[str, str]) -> str:
    """系統ごとの多数決をまとめる。全系統が合格なら pass。ある系統が合格で別の系統が合格でなければ判定不一致（合格にしない）。"""
    vals = set(majorities.values())
    if not vals:
        return "unknown"
    if vals == {"pass"}:
        return "pass"
    if "pass" in vals:
        return "judge_disagreement"
    return "fail" if "fail" in vals else "unknown"


def combine_rubric(majorities: dict[str, str], negate: bool = False) -> str:
    """系統ごとの多数決をまとめる。否定型（negate）は各系統の確定したラベル（pass / fail）を反転してから合意を求める
    （UNKNOWN は反転せず、確定票として扱わない。FAIL と UNKNOWN の組を、反転後に合格にしない）。"""
    if negate:
        flip = {"pass": "fail", "fail": "pass"}
        majorities = {f: flip.get(v, v) for f, v in majorities.items()}
    return combine(majorities)


def dedupe_calibration(items: list[Any], key: Callable[[Any], str], answer: Callable[[Any], bool]) -> list[Any] | None:
    """同じ盲検入力は 1 件にまとめる。正解が矛盾する場合は校正できないため None を返す。"""
    seen: dict[str, bool] = {}
    out = []
    for it in items:
        k = key(it)
        label = answer(it)
        if k in seen:
            if seen[k] != label:
                return None
        else:
            seen[k] = label
            out.append(it)
    return out


def pick_calibration(items: list[Any], n: int, seed: int, answer: Callable[[Any], bool]) -> list[Any]:
    """校正に使う項目を n 件まで選ぶ（seed で並べ替え、決定的な採点器の合格と不合格を交互に取る。片方に偏らないため）。"""
    rng = random.Random(f"calibration:{seed}")
    pos = [x for x in items if answer(x)]
    neg = [x for x in items if not answer(x)]
    rng.shuffle(pos)
    rng.shuffle(neg)
    out: list[Any] = []
    while len(out) < n and (pos or neg):
        for side in (pos, neg):
            if side and len(out) < n:
                out.append(side.pop())
    return out
