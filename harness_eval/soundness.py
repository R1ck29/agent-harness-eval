"""評価の結論（verdict）: 事前登録した規則で、候補がベースラインより良くなったかを 4 値で返す。モデルは呼ばない。

結論:
  IMPROVED     … 差（候補 − ベースライン）の区間の下端 > 0
  REGRESSED    … 差の区間の上端 < 0、または対照タスクではっきり後退（区間の上端 < 0）
  NO CHANGE    … 区間が 0 をまたぎ、かつ ±最小効果（min_effect）の内側に収まる（差がないと言えるだけの精度がある）
  INCONCLUSIVE … 区間が 0 をまたぎ ±最小効果を超える（検出力不足）、または妥当性のガードに掛かった

妥当性のガード（どれかに掛かれば INCONCLUSIVE。結論より優先）:
  - エラーで無効になった試行（利用上限・レート制限・認証・agent_error など）の割合 > max_error_rate
    （利用上限のメッセージは、旧版が agent_error と記録していても検出する）
  - ベースラインの合格率が床（floor 未満: 難しすぎる）か天井（ceiling 超: 易しすぎる）
  - 比べられたタスク数 < min_tasks、または 1 タスク・1 条件あたりの有効試行 < min_trials_per_task
  - 片側だけ値のあるタスクがある（悪化が隠れうる）
  - LLM 判定が合否を決めた試行があるのに: 同系統の判定役しかなかった／校正の一致率が足りない・校正がない
  - 対照タスクの後退の疑い（推定 < −control_margin だが区間が 0 をまたぐ）

採用できない（adoptable = False。結論は「暫定」）:
  - dev 段階の判定役の LLM 判定が合否を決めた試行がある（LLM 判定が合否を決めていなければ、判定役の段階は結論に効かない）
  - LLM 判定が合否を決めた試行があり、判定役が弱い（--allow-weak-judge）・盲検化していない（旧版の結果）
  - 事前登録した規則を変えて集計し直した（事後）

区間: タスクでクラスタした 2 段ブートストラップ（タスクを復元抽出し、選ばれたタスクの中で各条件の試行も復元抽出する）の
パーセンタイル区間。タスク間のばらつきとタスク内の試行のばらつきの両方を含む。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import asdict, dataclass, fields
from statistics import mean
from typing import Any

from .judging import TIER_VOTES, model_strength, other_family, strength_ok
from .metrics import sign_test
from .record import EXCLUDED_ERRORS, TrialRecord
from .spec import SpecError

CONCLUSIONS = ("IMPROVED", "NO CHANGE", "REGRESSED", "INCONCLUSIVE")
# 終了コード: 0 は「採用してよい改善」だけ。それ以外はマージ・採用不可
EXIT = {"IMPROVED": 0, "REGRESSED": 1, "INCONCLUSIVE": 3, "NO CHANGE": 4}
EXIT_NOT_ADOPTABLE = 5  # IMPROVED だが暫定（dev 段階・弱い判定役・事後の規則）
CI_METHOD = ("タスクでクラスタした 2 段ブートストラップ（タスクを復元抽出し、その中で各条件の試行も復元抽出）の"
             "パーセンタイル区間。差はタスクごとの合格率の差の平均")
USAGE_LIMIT_RE = re.compile(r"usage limit|hit your (?:usage|rate) limit|insufficient_quota|exceeded your current quota|"
                            r"credit balance is too low|out of credits|purchase more credits", re.IGNORECASE)
# 評価対象の失敗ではなく、エラーで結果が得られなかった試行（max_turns・timeout・budget_exceeded はエージェントの振る舞いなので含めない）
INVALIDATING = set(EXCLUDED_ERRORS) | {"agent_error", "agent_crash", "usage_limit", "budget_stop"}
JUDGED = ("pass", "fail", "unknown", "judge_disagreement")


@dataclass(frozen=True)
class Rules:
    """判定規則（実行前に固定して事前登録する）。"""
    baseline: str | None = None
    candidate: str | None = None  # None: ベースライン以外のすべて
    control_tags: tuple[str, ...] = ()  # このタグのタスクは「対照」（後退していないことだけを見る）
    metric: str = "pass_at_1"
    floor: float = 0.2
    ceiling: float = 0.8
    min_tasks: int = 5
    recommended_tasks: int = 10
    min_trials_per_task: int = 2
    min_effect: float = 0.10  # 最小の意味のある効果。区間がこの内側なら NO CHANGE
    confidence: float = 0.95
    max_error_rate: float = 0.2
    control_margin: float = 0.10
    min_calibration_agreement: float = 0.8
    min_calibration_items: int = 6
    required_tier: str = "decisive"
    resamples: int = 10_000
    seed: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.floor < self.ceiling <= 1:
            raise SpecError("floor < ceiling で、どちらも 0〜1")
        if self.min_tasks < 2 or self.min_trials_per_task < 1:
            raise SpecError("min_tasks は 2 以上、min_trials_per_task は 1 以上")
        if self.resamples < 1 or self.min_calibration_items < 1:
            raise SpecError("resamples と min_calibration_items は 1 以上")
        if not 0 < self.confidence < 1 or not 0 <= self.max_error_rate <= 1 or not 0 <= self.min_calibration_agreement <= 1:
            raise SpecError("confidence・max_error_rate・min_calibration_agreement は 0〜1")
        if not 0 < self.min_effect < 1 or self.control_margin < 0:
            raise SpecError("min_effect は 0〜1、control_margin は 0 以上")
        if self.required_tier not in ("dev", "decisive"):
            raise SpecError("required_tier は dev / decisive")
        if self.metric != "pass_at_1":
            raise SpecError("verdict の指標は pass_at_1 だけです")
        object.__setattr__(self, "control_tags", tuple(self.control_tags))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["control_tags"] = list(self.control_tags)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rules":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise SpecError(f"規則に未知のキー {sorted(unknown)}（使えるキー: {sorted(known)}）")
        return cls(**{k: (tuple(v) if k == "control_tags" else v) for k, v in d.items()})

    def hash(self) -> str:
        return rules_hash(self.to_dict())


def rules_hash(d: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 試行の扱い
def invalid_reason(r: TrialRecord) -> str | None:
    """試行がエラーで結果を得られなかったなら理由（エラーの種類）。利用上限は旧版が agent_error と記録していても検出する。"""
    e = r.error or {}
    t = e.get("type")
    if t and USAGE_LIMIT_RE.search(str(e.get("message", ""))):
        return "usage_limit"
    if not r.valid:
        return t or "invalid"
    return t if t in INVALIDATING else None


def outcome(r: TrialRecord) -> bool | None:
    """比較に使う合否（配布物がないと通らない判定を除いた合否。旧形式は全判定の合否）。
    判定役の不一致（judge_disagreement）がある試行は、記録の合否にかかわらず合格として数えない。"""
    if r.cmp_passed is None and not r.cmp_legacy:
        return None
    if any(g.get("type") == "llm" and g.get("status") == "judge_disagreement" for g in r.grader_results or []):
        return False
    return bool(r.passed if r.cmp_passed is None else r.cmp_passed)


def llm_decided(r: TrialRecord) -> bool:
    """LLM 判定が実行され、合否に効いた試行か（コード判定が全合格の試行だけで LLM 判定を走らせるので、走れば決定的）。"""
    return any(g.get("type") == "llm" and g.get("status") in JUDGED for g in r.grader_results or [])


def cluster_bootstrap(pairs: list[tuple[list[bool], list[bool]]], confidence: float, resamples: int,
                      seed: int) -> tuple[float | None, float | None, float | None]:
    """pairs: タスクごとの（ベースラインの合否列, 候補の合否列）。差 = 候補 − ベースライン の平均と区間。"""
    n = len(pairs)
    if n == 0:
        return None, None, None
    rate = lambda xs: sum(xs) / len(xs)  # noqa: E731
    est = mean(rate(c) - rate(b) for b, c in pairs)
    if n == 1 and all(len(b) < 2 and len(c) < 2 for b, c in pairs):
        return est, None, None
    rng = random.Random(seed)
    stats = []
    for _ in range(resamples):
        tot = 0.0
        for _ in range(n):
            b, c = pairs[rng.randrange(n)]
            pb, pc = rate(b), rate(c)
            tot += sum(rng.random() < pc for _ in c) / len(c) - sum(rng.random() < pb for _ in b) / len(b)
        stats.append(tot / n)
    stats.sort()
    a = (1 - confidence) / 2
    return est, stats[int(a * resamples)], stats[min(int((1 - a) * resamples), resamples - 1)]


# ---------------------------------------------------------------- 判定役の記録
def judge_info(summary: dict[str, Any]) -> dict[str, Any]:
    """summary.json の判定役の設定。旧版の結果は options から復元する（旧版は盲検化なし・1 回・判定役は 1 系統）。"""
    meta = summary.get("meta", {})
    if isinstance(meta.get("judge"), dict):
        return meta["judge"]
    o = meta.get("options", {})
    fam, model = o.get("judge_agent") or "claude_code", o.get("judge_model")
    return {"legacy": True, "llm": bool(o.get("llm")), "tier": None, "models": {fam: model},
            "strength": {fam: model_strength(model)}, "weak_allowed": True, "blind": False, "votes": 1,
            "plan": {}, "calibration": None}


def _judge_checks(j: dict[str, Any], agents: set[str], decided: int, disagreements: int, rules: Rules,
                  checks: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """LLM 判定に関するチェック。戻り値: (INCONCLUSIVE にする理由, 採用できない理由)。"""
    fail: list[str] = []
    prov: list[str] = []
    tier = j.get("tier")
    below = tier == "dev" and rules.required_tier == "decisive"
    if below and decided:
        prov.append("dev 段階: 採用判断には使えません（--tier decisive で実行し直してください）")
        checks.append(_c("tier", "provisional", "dev 段階（中位モデル・1 回）の LLM 判定が合否を決めた試行があります。"
                         "結論は暫定で、採用・マージの根拠にできません"))
    elif tier == "dev":
        checks.append(_c("tier", "ok", "dev 段階。ただし LLM 判定が合否を決めた試行はないため、結論は判定役の段階に依存しません"))
    else:
        checks.append(_c("tier", "ok" if tier else "n/a", f"段階 {tier or '記録なし（旧版）'}"))
    models = j.get("models") or {}
    weak = sorted(f"{f}: {m}" for f, m in models.items() if not strength_ok(m, "dev" if tier == "dev" else "decisive"))
    if weak and j.get("llm", True):
        checks.append(_c("judge_strength", "provisional" if decided else "warn",
                         f"弱い判定役（{', '.join(weak)}）。" + ("採用判断には使えません" if decided else "ただし LLM 判定は合否を決めていません")))
    if not decided:
        checks.append(_c("calibration", "n/a", "LLM 判定が合否を決めた試行はありません（校正は不要）"))
        checks.append(_c("judge_family", "n/a", "LLM 判定を使っていないため判定役の系統は結論に影響しません"))
        return fail, prov
    if weak or not models:
        prov.append(f"弱い判定役（{', '.join(weak) or 'モデル不明'}）の判定が合否を決めた試行があります")
    else:
        checks.append(_c("judge_strength", "ok", f"判定モデル {models}"))
    if tier != "dev" and (j.get("votes") or 0) < TIER_VOTES["decisive"]:
        prov.append("採用判断には 1 系統あたり 3 回以上の判定が必要です")
    if tier == "dev" and rules.required_tier == "dev":
        prov.append("dev 段階の判定は、規則で dev を許しても採用判断には使えません")
    if tier not in ("dev", "decisive"):
        prov.append("decisive 段階の記録を確認できません")
    if not j.get("blind"):
        prov.append("判定役は盲検化されていません（旧版の結果）")
        checks.append(_c("blind", "provisional", "盲検化なし（旧版の結果）"))
    else:
        checks.append(_c("blind", "ok", f"盲検化あり・判定順はランダム（seed {j.get('seed')}）"))
    plan = j.get("plan") or {}
    routes = {a: set(models if j.get("legacy") else (plan.get(a) or {}).get("families") or ()) for a in agents}
    same = sorted(a for a, families in routes.items() if families == {a})
    missing = sorted(a for a, families in routes.items() if a not in same and
                     (other_family(a) not in families or not families <= models.keys()))
    if same:
        fail.append(f"同系統の判定役しかありません（評価対象 {same}）。LLM 判定が合否を決めた試行が {decided} 件あります")
        checks.append(_c("judge_family", "fail", f"同系統の判定役（{same}）。別系統の CLI を入れてください"))
    if missing:
        fail.append(f"別系統の判定役を確認できません（評価対象 {missing}）")
        checks.append(_c("judge_route", "fail", fail[-1]))
    if not same and not missing:
        both = all(len(families) == 2 for families in routes.values())
        checks.append(_c("judge_family", "ok", "両系統一致" if both else "別系統の判定役"))
    if disagreements:
        checks.append(_c("judge_disagreement", "warn", f"判定不一致 {disagreements} 件（合格として数えていません）"))
    cal = j.get("calibration") or {}
    items, rate = int(cal.get("items") or 0), cal.get("rate")
    if cal.get("conflicting_answers"):
        fail.append("同じ盲検入力の正解が合格と不合格で矛盾しているため、判定役を校正できません")
        checks.append(_c("calibration", "fail", fail[-1]))
    elif items < rules.min_calibration_items or rate is None:
        fail.append(f"判定役の校正が不足しています（校正項目 {items} 件、必要 {rules.min_calibration_items} 件）")
        checks.append(_c("calibration", "fail", f"校正項目 {items} 件 < {rules.min_calibration_items} 件。"
                         "決定的な採点器を持つタスクに calibration を書いてください"))
    elif rate < rules.min_calibration_agreement:
        fail.append(f"判定役と決定的な採点器の一致率 {rate:.0%} が {rules.min_calibration_agreement:.0%} 未満です")
        checks.append(_c("calibration", "fail", f"一致率 {rate:.0%}（{cal.get('agree')}/{items}）< {rules.min_calibration_agreement:.0%}"))
    else:
        np_, nf = cal.get("pass_items"), cal.get("fail_items")
        ap, af = cal.get("agree_pass"), cal.get("agree_fail")
        if np_ is None or nf is None or ap is None or af is None:
            fail.append("判定役の校正の内訳（合格例・不合格例ごとの一致）が記録されていません")
            checks.append(_c("calibration", "fail", fail[-1]))
        elif np_ < 1 or nf < 1:
            fail.append(f"判定役の校正に合格例・不合格例の両方が必要です（合格例 {np_} 件・不合格例 {nf} 件）。"
                        "独立した校正項目が足りないため採用判定を保留します")
            checks.append(_c("calibration", "fail", fail[-1]))
        elif ap / np_ < rules.min_calibration_agreement or af / nf < rules.min_calibration_agreement:
            fail.append(f"判定役の一致率が合格例 {ap}/{np_}・不合格例 {af}/{nf} で、どちらかが {rules.min_calibration_agreement:.0%} 未満です")
            checks.append(_c("calibration", "fail", fail[-1]))
        else:
            checks.append(_c("calibration", "ok", f"一致率 {rate:.0%}（{cal.get('agree')}/{items}。合格例 {ap}/{np_}・不合格例 {af}/{nf}）"))
    return fail, prov


def _integrity(recs: list[TrialRecord], cand: str, inconclusive: list[str], rules: Rules | None = None) -> dict[str, Any]:
    """leak（INVALID。有効率に入る）・tamper（不合格）・隔離なしの試行を数える。隔離のない試行で候補が答えを見に行った形跡があれば、
    トランスクリプトに出ない読み方もありうるので結論を出さない（隔離ありなら、見に行っても見えないので警告だけ）。"""
    leak = [r for r in recs if invalid_reason(r) == "leak"]
    tamper = [r for r in recs if "test_tamper" in (r.failure_tags or [])]
    modes = [(r.meta or {}).get("sandbox") for r in recs]
    open_ = sum(1 for m in modes if m in ("off", "unsupported"))
    unknown = sum(1 for m in modes if m is None)
    cand_open_leak = [r for r in leak if r.profile_id == cand and (r.meta or {}).get("sandbox") != "bwrap"]
    parts = [f"答え・試行の外の参照（INVALID） {len(leak)} 件", f"見えているテストの改ざん（不合格） {len(tamper)} 件",
             f"隔離なしの試行 {open_} 件" + (f"（記録なし {unknown} 件）" if unknown else "")]
    open_grader = sum(1 for r in recs if (r.meta or {}).get("grader_isolation") != "bwrap"
                      and any(g.get("type") == "command" and g.get("status") not in ("not_run", "skipped_prior_fail")
                              for g in r.grader_results or []))
    if open_grader:
        parts.append(f"隔離なし・未確認の採点 {open_grader} 件")
    msg = "、".join(parts)
    if open_grader and (rules is None or rules.required_tier == "decisive"):
        inconclusive.append(f"採点コマンドの隔離が確認できない試行が {open_grader} 件あります。候補コードがホストの権限で"
                            "動いた可能性があるため、decisive では結論を保留します（Linux + bwrap で実行し直してください）")
        return _c("integrity", "fail", inconclusive[-1] + f"（{msg}）")
    if cand_open_leak:
        inconclusive.append(f"隔離なしの試行で、候補が評価の答え・試行の外を参照しました（{len(cand_open_leak)} 件）。"
                            "検出できない読み方もありうるため結論を出しません")
        return _c("integrity", "fail", inconclusive[-1] + f"（{msg}）")
    return _c("integrity", "warn" if (leak or tamper or open_ or open_grader) else ("n/a" if unknown == len(recs) else "ok"), msg)


def _c(cid: str, status: str, message: str) -> dict[str, Any]:
    return {"id": cid, "status": status, "message": message}


# ---------------------------------------------------------------- 本体
def _task_pairs(recs: list[TrialRecord], base: str, cand: str, tids: set[str]) -> tuple[dict[str, tuple[list[bool], list[bool]]], list[str]]:
    by: dict[tuple[str, str], list[bool]] = {}
    seen: dict[str, set[str]] = {}
    for r in recs:
        if r.task_id not in tids or r.profile_id not in (base, cand):
            continue
        seen.setdefault(r.task_id, set()).add(r.profile_id)
        if invalid_reason(r) is None and (o := outcome(r)) is not None:
            by.setdefault((r.task_id, r.profile_id), []).append(o)
    pairs = {t: (by[(t, base)], by[(t, cand)]) for t in sorted(tids) if (t, base) in by and (t, cand) in by}
    one_sided = sorted(t for t in tids if (((t, base) in by) != ((t, cand) in by)) or
                       (t in seen and len(seen[t]) == 1))
    return pairs, one_sided


def verdict(records: list[TrialRecord], summary: dict[str, Any], rules: Rules,
            prereg: dict[str, Any] | None = None) -> dict[str, Any]:
    """1 組（rules.baseline と rules.candidate）の結論。prereg: 実行前に保存した事前登録（prereg.json の中身）。"""
    base, cand = rules.baseline, rules.candidate
    if not base or not cand:
        raise SpecError("verdict にはベースラインと候補のプロファイルが要ります")
    profiles = {r.profile_id for r in records}
    for pid in (base, cand):
        if pid not in profiles:
            raise SpecError(f"{pid}: 結果にこのプロファイルの試行がありません（あるもの: {sorted(profiles)}）")
    meta_tasks = summary.get("meta", {}).get("tasks", {})
    recs = [r for r in records if r.profile_id in (base, cand)]
    all_tids = {r.task_id for r in recs}
    ctrl = {t for t in all_tids if set((meta_tasks.get(t) or {}).get("tags") or []) & set(rules.control_tags)}
    target = all_tids - ctrl
    checks: list[dict[str, Any]] = []
    inconclusive: list[str] = []

    # 1. エラーで無効になった試行
    bad = [(r, why) for r in recs if (why := invalid_reason(r))]
    by_type: dict[str, int] = {}
    for _, why in bad:
        by_type[why] = by_type.get(why, 0) + 1
    err_rate = len(bad) / len(recs) if recs else 1.0
    errors = {"invalid": len(bad), "total": len(recs), "rate": err_rate, "by_type": by_type}
    if err_rate > rules.max_error_rate:
        inconclusive.append(f"エラーで無効になった試行が {len(bad)}/{len(recs)}（{err_rate:.0%}）で、上限 {rules.max_error_rate:.0%} を超えています"
                            f"（{', '.join(f'{k} {v}' for k, v in sorted(by_type.items()))}）")
        checks.append(_c("errors", "fail", inconclusive[-1]))
    else:
        checks.append(_c("errors", "warn" if bad else "ok", f"無効な試行 {len(bad)}/{len(recs)}（{err_rate:.0%}）{by_type or ''}"))

    # 1b. ズル対策（評価の答えを見た形跡・見えているテストの改ざん・隔離の有無）
    checks.append(_integrity(recs, cand, inconclusive, rules))

    # 1c. 必須の LLM 判定が未実施（--no-llm）の試行。未実施の条件で勝敗が変わり得るので採用に使わない
    skipped = [r for r in recs if invalid_reason(r) is None
               and any(g.get("type") == "llm" and g.get("status") == "not_run" for g in r.grader_results or [])]
    if skipped:
        inconclusive.append(f"必須の LLM 判定が未実施（--no-llm）の試行が {len(skipped)} 件あります。判定を実行していない条件で"
                            "合否が変わり得るため、結論を出しません（--no-llm はスモーク確認用です）")
        checks.append(_c("required_conditions", "fail", inconclusive[-1]))

    # 2. 比較（対象タスク）
    pairs, one_sided = _task_pairs(recs, base, cand, target)
    # 予定した対象タスクのうち、両条件とも有効な試行が 0 件のもの（全滅したタスクがデータ不足の判定から消えないように）
    no_valid = sorted(t for t in target if t not in pairs and t not in one_sided)
    if no_valid:
        inconclusive.append(f"有効な試行が 1 件もないタスクがあります（両条件とも）: {no_valid[:5]}（予定した対象タスク {len(target)} 件中 {len(no_valid)} 件）")
        checks.append(_c("missing_tasks", "fail", inconclusive[-1]))
    n_tasks = len(pairs)
    base_rate = mean(sum(b) / len(b) for b, _ in pairs.values()) if pairs else None
    cand_rate = mean(sum(c) / len(c) for _, c in pairs.values()) if pairs else None
    est, low, high = cluster_bootstrap(list(pairs.values()), rules.confidence, rules.resamples, rules.seed)
    min_trials = min((min(len(b), len(c)) for b, c in pairs.values()), default=0)
    if ctrl and not target:
        inconclusive.append("対象タスクがありません（すべて対照タスクです）")
    if base_rate is None:
        inconclusive.append("比べられるタスクがありません")
        checks.append(_c("baseline_range", "fail", "ベースラインの合格率を計算できません"))
    elif base_rate > rules.ceiling:
        inconclusive.append(f"ベースラインの合格率 {base_rate:.0%} が天井（{rules.ceiling:.0%} 超）です。タスクが易しすぎて改善を測れません")
        checks.append(_c("baseline_range", "fail", inconclusive[-1]))
    elif base_rate < rules.floor:
        inconclusive.append(f"ベースラインの合格率 {base_rate:.0%} が床（{rules.floor:.0%} 未満）です。タスクが難しすぎて差を測れません")
        checks.append(_c("baseline_range", "fail", inconclusive[-1]))
    else:
        checks.append(_c("baseline_range", "ok", f"ベースラインの合格率 {base_rate:.0%}（{rules.floor:.0%}〜{rules.ceiling:.0%}）"))
    if n_tasks < rules.min_tasks or min_trials < rules.min_trials_per_task:
        inconclusive.append(f"データが足りません: 比べられたタスク {n_tasks} 件（最低 {rules.min_tasks}）、"
                            f"1 タスク・1 条件あたりの有効試行 最少 {min_trials} 回（最低 {rules.min_trials_per_task}）")
        checks.append(_c("sample_size", "fail", inconclusive[-1]))
    elif n_tasks < rules.recommended_tasks:
        checks.append(_c("sample_size", "warn", f"タスク {n_tasks} 件は推奨 {rules.recommended_tasks} 件未満です。区間は実際より狭く出やすい"))
    else:
        checks.append(_c("sample_size", "ok", f"タスク {n_tasks} 件、1 タスク・1 条件あたり最少 {min_trials} 試行"))
    if one_sided:
        inconclusive.append(f"片側の条件にしか有効な結果がないタスクがあります: {one_sided[:5]}（悪化が隠れうる）")
        checks.append(_c("one_sided", "fail", inconclusive[-1]))
    if low is not None and high is not None and low <= 0 <= high:
        half = (high - low) / 2
        checks.append(_c("power", "ok" if (low > -rules.min_effect and high < rules.min_effect) else "warn",
                         f"区間 [{low:+.2f}, {high:+.2f}] が 0 をまたぐ（半幅 {half:.2f}、最小効果 ±{rules.min_effect:.2f}）"))

    # 3. 対照タスク
    controls = None
    regressed_ctrl = False
    missing_tags = sorted(tag for tag in rules.control_tags if not any(
        tag in ((meta_tasks.get(t) or {}).get("tags") or []) for t in ctrl
    ))
    if missing_tags:
        inconclusive.append(f"対照タグ {missing_tags} のタスクがありません")
        checks.append(_c("controls_missing", "fail", inconclusive[-1]))
    if ctrl:
        cpairs, _ = _task_pairs(recs, base, cand, ctrl)
        c_est, c_low, c_high = cluster_bootstrap(list(cpairs.values()), rules.confidence, rules.resamples, rules.seed)
        controls = {"tasks": len(cpairs), "delta": c_est, "ci": [c_low, c_high] if c_low is not None else None,
                    "regressed_tasks": sorted(t for t, (b, c) in cpairs.items() if sum(c) / len(c) < sum(b) / len(b) - 1e-9)}
        if c_high is not None and c_high < 0:
            regressed_ctrl = True
            checks.append(_c("controls", "fail", f"対照タスクで後退（差 {c_est:+.2f}、区間 [{c_low:+.2f}, {c_high:+.2f}]）"))
        elif c_est is not None and c_est < -rules.control_margin:
            span = "区間が 0 をまたぐ" if c_low is not None else "区間を計算できない"
            inconclusive.append(f"対照タスクの後退の疑い（差 {c_est:+.2f} < −{rules.control_margin:.2f}、{span}）")
            checks.append(_c("controls", "fail", inconclusive[-1]))
        elif missing_controls := sorted(t for t in ctrl if t not in cpairs or
                                        min(map(len, cpairs[t])) < rules.min_trials_per_task):
            inconclusive.append(f"対照タスクの有効な比較が不足しています: {missing_controls[:5]}")
            checks.append(_c("controls", "fail", inconclusive[-1]))
        else:
            checks.append(_c("controls", "ok", f"対照タスク {len(cpairs)} 件で後退なし（差 {c_est if c_est is None else round(c_est, 3)}）"))

    # 4. 判定役
    j = judge_info(summary)
    decided = sum(1 for r in recs if invalid_reason(r) is None and llm_decided(r))
    disagreements = sum(1 for r in recs for g in r.grader_results or [] if g.get("status") == "judge_disagreement")
    agents = {r.agent for r in recs if invalid_reason(r) is None and llm_decided(r)}
    j_fail, prov = _judge_checks(j, agents, decided, disagreements, rules, checks)
    inconclusive += j_fail

    # 5. 事前登録
    reg = _prereg_status(rules, prereg)
    if reg["status"] == "invalid":
        inconclusive.append("事前登録のハッシュを検証できません")
        checks.append(_c("prereg", "fail", inconclusive[-1]))
    elif reg["status"] == "post_hoc":
        prov.append("事前登録と違う規則で集計し直しています（事後）")
        checks.append(_c("prereg", "provisional", f"事後: 事前登録 {reg.get('registered_hash')} と違う規則 {reg['hash']}（違い: {reg.get('changed')}）"))
    elif reg["status"] == "registered":
        checks.append(_c("prereg", "ok", f"事前登録どおり（規則のハッシュ {reg['hash']}）"))
    else:
        checks.append(_c("prereg", "warn", f"事前登録の記録がありません（旧版の結果）。既定の規則で判定（ハッシュ {reg['hash']}）"))

    # 6. 結論
    if inconclusive:
        conclusion, reasons = "INCONCLUSIVE", inconclusive
    elif regressed_ctrl:
        conclusion, reasons = "REGRESSED", ["対照タスクで後退しました"]
    elif low is None or high is None:
        conclusion, reasons = "INCONCLUSIVE", ["区間を計算できません"]
    elif low > 0:
        conclusion, reasons = "IMPROVED", [f"差 {est:+.2f} の {rules.confidence:.0%} 区間 [{low:+.2f}, {high:+.2f}] が 0 より上"]
    elif high < 0:
        conclusion, reasons = "REGRESSED", [f"差 {est:+.2f} の {rules.confidence:.0%} 区間 [{low:+.2f}, {high:+.2f}] が 0 より下"]
    elif low > -rules.min_effect and high < rules.min_effect:
        conclusion, reasons = "NO CHANGE", [f"区間 [{low:+.2f}, {high:+.2f}] が 0 をまたぎ、最小効果 ±{rules.min_effect:.2f} の内側"]
    else:
        conclusion, reasons = "INCONCLUSIVE", [f"検出力不足: 区間 [{low:+.2f}, {high:+.2f}] が 0 をまたぎ、最小効果 ±{rules.min_effect:.2f} を超えて広い"]
    provisional = bool(prov) and conclusion == "IMPROVED"
    adoptable = conclusion == "IMPROVED" and not prov
    improved_t = sorted(t for t, (b, c) in pairs.items() if sum(c) / len(c) > sum(b) / len(b) + 1e-9)
    regressed_t = sorted(t for t, (b, c) in pairs.items() if sum(c) / len(c) < sum(b) / len(b) - 1e-9)
    return {
        "baseline": base, "candidate": cand, "conclusion": conclusion, "label": label(conclusion, provisional),
        "adoptable": adoptable, "provisional": provisional, "not_adoptable_reasons": prov,
        "exit_code": exit_code(conclusion, adoptable), "reasons": reasons,
        "baseline_rate": base_rate, "candidate_rate": cand_rate, "delta": est,
        "ci": [low, high] if low is not None else None, "confidence": rules.confidence, "ci_method": CI_METHOD,
        "n_tasks": n_tasks, "target_tasks": sorted(target), "control_tasks": sorted(ctrl), "min_trials_per_task": min_trials,
        "improved_tasks": improved_t, "regressed_tasks": regressed_t, "sign_p": sign_test(len(improved_t), len(regressed_t)),
        "controls": controls, "errors": errors, "llm_decided_trials": decided, "judge_disagreements": disagreements,
        "judge": _judge_summary(j), "checks": checks, "rules": rules.to_dict(), "rules_hash": rules.hash(), "prereg": reg,
    }


def _prereg_status(rules: Rules, prereg: dict[str, Any] | None) -> dict[str, Any]:
    """事前登録との照合。登録が candidate を決めていなければ（ベースライン以外すべて）、候補の選択は規則の変更とみなさない。"""
    cur = rules.to_dict()
    if not prereg:
        return {"status": "none", "hash": rules_hash(cur)}
    reg = dict(prereg.get("rules") or {})
    if prereg.get("rules_hash") != rules_hash(reg):
        return {"status": "invalid", "hash": rules_hash(cur), "registered_hash": prereg.get("rules_hash")}
    for k in ("baseline", "candidate"):  # 登録時に未定（自動選択）だった比較の組は、規則の変更とみなさない
        if reg.get(k) is None:
            reg[k] = cur[k]
    changed = sorted(k for k in set(reg) | set(cur) if reg.get(k) != cur.get(k))
    if changed:
        return {"status": "post_hoc", "hash": rules_hash(cur), "registered_hash": prereg.get("rules_hash"), "changed": changed,
                "registered_at": prereg.get("registered_at")}
    return {"status": "registered", "hash": rules_hash(cur), "registered_hash": prereg.get("rules_hash"),
            "registered_at": prereg.get("registered_at")}


def _judge_summary(j: dict[str, Any]) -> dict[str, Any]:
    cal = j.get("calibration") or {}
    out: dict[str, Any] = {"tier": j.get("tier"), "models": j.get("models"), "strength": j.get("strength"), "blind": j.get("blind"),
                           "votes": j.get("votes"), "seed": j.get("seed"), "plan": j.get("plan"), "legacy": bool(j.get("legacy")),
                           "llm": j.get("llm"), "weak_allowed": j.get("weak_allowed"),
                           "calibration_rate": cal.get("rate"), "calibration_items": cal.get("items")}
    if j.get("actual_models"):
        out["actual_models"] = j["actual_models"]
    return out


def label(conclusion: str, provisional: bool) -> str:
    return f"{conclusion}（暫定: 採用判断には使えません）" if provisional else conclusion


def exit_code(conclusion: str, adoptable: bool) -> int:
    if conclusion == "IMPROVED" and not adoptable:
        return EXIT_NOT_ADOPTABLE
    return EXIT[conclusion]


def default_baseline(summary: dict[str, Any]) -> str | None:
    """配布物なし（install が空）のプロファイルが 1 つだけならそれ。"""
    empty = [pid for pid, v in (summary.get("meta", {}).get("profiles") or {}).items() if v.get("install_empty")]
    return empty[0] if len(empty) == 1 else None


def run_verdicts(records: list[TrialRecord], summary: dict[str, Any], rules: Rules,
                 prereg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """実行のあとの結論。ベースラインが決まらなければ空。候補が未定ならベースライン以外のすべてのプロファイルについて。"""
    from dataclasses import replace
    base = rules.baseline or default_baseline(summary)
    profiles = sorted({r.profile_id for r in records})
    if not base or base not in profiles:
        return []
    cands = [rules.candidate] if rules.candidate else [p for p in profiles if p != base]
    return [verdict(records, summary, replace(rules, baseline=base, candidate=c), prereg) for c in cands if c in profiles]
