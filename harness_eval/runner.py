"""実行の司令塔: タスク × プロファイル × 試行回数 を回し、記録・採点・集計する。

トークン節約の仕組み（評価の正しさを損なわないものだけ）:
- キャッシュ: タスク・フィクスチャ・グレーダー・プロファイル・配布物・CLI 版・試行番号・経路の変数・判定設定が
  すべて同じ試行は、過去の有効な結果を再利用する（変更していない組み合わせは再実行しない）
- LLM 判定は、コード判定がすべて合格した試行にだけ実行する（配布物がないと通らない判定の不合格は除く。合否は変わらない）
- 試行ごと（max_turns / max_budget_usd / timeout_s）・実行全体（--max-cost-usd）・口座ごとの月（--monthly-budget-usd）に上限
- 異常終了（レート制限など）だけ再試行する。失敗した試行を「良い結果が出るまで」再実行はしない
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__, credentials
from .adapters import classify_stderr, get_adapter
from .cheatproof import capture_tests, leak_check, summarize_hits, tamper_check, tamper_result
from .costs import add_usage, empty_usage, solver_call_estimate, solver_cost, usage_summary
from .graders import JudgeConfig, grade_all, judge_content, rescore
from .judge_phase import JudgePhase
from .judging import FAMILIES, JudgeSettings, make_settings
from .isolation import (fresh_dir, ancestor_instructions, build_env, bwrap_problem, copy_credentials, fill, kill_running, make_trial_paths,
                        placeholders, release_home, route_signature, run_install, run_process, sandbox_cmd, sandbox_mode, snapshot)
from .metrics import summarize
from .record import EXCLUDED_ERRORS, TrialRecord
from .spec import NO_SPLIT, ROUTE_VARS, Profile, SpecError, Task, load_profile, reject_in_place, split_mixing

NOT_RUN_ERRORS = {"budget_stop"}

CACHE_SCHEMA = "7"  # 試行レコードの形式を変えたら上げる（古いキャッシュを使わないため）。7: 隔離・leak・tamper・環境変数の許可リスト


@dataclass
class RunOptions:
    trials: int = 1
    k: int | None = None
    tags: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    splits: list[str] = field(default_factory=list)  # train / dev / holdout / none（分割なし）。空ならすべて
    jobs: int = 1
    llm: bool = True
    tier: str = "dev"  # dev（中位モデル 1 回・暫定）/ decisive（強いモデル・3 回・両系統一致。採用判断用）
    judge_agent: str | None = None  # 判定役の系統を 1 つに固定する（既定: 評価対象の別系統。decisive で両方あれば両系統）
    judge_models: dict[str, str] = field(default_factory=dict)  # 系統 → 判定モデルの上書き
    judge_votes: int | None = None  # 1 系統あたりの判定回数の上書き（既定: dev 1 / decisive 3）
    allow_weak_judge: bool = False
    judge_families: list[str] | None = None  # 使ってよい判定役の系統（既定: CLI が見つかった系統）
    calibration_items: int = 6
    judge_executable: list[str] | None = None  # 判定 CLI の実行ファイル（--judge-agent の系統。なければ claude_code）
    judge_executables: dict[str, list[str]] = field(default_factory=dict)  # 系統 → 判定 CLI の実行ファイル
    prereg: dict[str, Any] | None = None  # 事前登録する判定規則（Rules.to_dict()）。run の開始前に prereg.json に保存する
    max_cost_usd: float | None = None
    monthly_budget_usd: float | None = None
    ledger: Path | None = None  # 費用台帳（既定: 出力先/spend.jsonl）
    retry_infra: int = 1
    retry_wait_s: float = 20.0
    # 判定の認証・環境変数を借りるプロファイル。id か YAML のパス、系統ごとなら 系統=参照（既定: 自動選択）。複数可
    judge_profile: list[str] = field(default_factory=list)
    trust_judge_input: bool = False  # credentials: in_place の判定に候補の出力を渡してよいと宣言する（CLI のツールが実認証を読める限界の承知）
    use_cache: bool = True
    cache_dir: Path = Path(".harness-eval-cache")
    keep: str = "failed"  # all / failed / none（作業ディレクトリを残すか）
    baseline: str | None = None
    seed: int = 0  # プロファイルの実行順の入れ替え（schedule）に使う
    log: Callable[[str], None] = print

    @property
    def kk(self) -> int:
        return self.k or self.trials


def select_pairs(tasks: list[Task], profiles: list[Profile], opt: RunOptions) -> list[tuple[Task, Profile]]:
    out = []
    for p in profiles:
        for t in tasks:
            if p.agent not in t.targets:
                continue
            if opt.tags and not set(opt.tags) & set(t.tags):
                continue
            if opt.task_ids and t.id not in opt.task_ids:
                continue
            if opt.splits and (t.split or NO_SPLIT) not in opt.splits:
                continue
            out.append((t, p))
    return out


def schedule(pairs: list[tuple[Task, Profile]], trials: int, seed: int) -> list[tuple[Task, Profile, int]]:
    """実行順。同じ（試行, タスク）の各プロファイルを続けて実行し、先に走るプロファイルを試行ごとに巡回させる
    （タスク内で各プロファイルが先頭になる回数がそろう）。プロファイルをまとめて順に回すと、時間とともに変わる条件
    （API の混雑・モデルの更新）が差に混ざるため。巡回の起点はタスクごとにずらし、seed で決める（同じ seed なら同じ順）。"""
    by_task: dict[str, list[tuple[Task, Profile]]] = {}
    for t, p in pairs:
        by_task.setdefault(t.id, []).append((t, p))
    rng = random.Random(seed)
    order = sorted({p.id for _, p in pairs})
    rng.shuffle(order)
    offsets = {tid: rng.randrange(len(items)) for tid, items in by_task.items()}
    jobs: list[tuple[Task, Profile, int]] = []
    for i in range(trials):
        for tid, items in by_task.items():
            items = sorted(items, key=lambda tp: order.index(tp[1].id))
            r = (i + offsets[tid]) % len(items)
            jobs += [(t, p, i) for t, p in items[r:] + items[:r]]
    return jobs


HOLDOUT_LEDGER = "holdout_uses.jsonl"


def holdout_row(run_id: str, tasks: list[Task], profiles: list[Profile]) -> dict[str, Any]:
    """holdout 利用台帳の 1 行。id とハッシュだけを書く（プロンプト・応答・ファイル内容は書かない）。
    tasks: holdout のタスク id → 中身のハッシュ。profiles: プロファイル id → 配布物のハッシュ（harness_hash。id を変えても
    同じ配布物は同じ値）と配布物なしかどうか。promote が「この holdout で何個の候補を見たか」を数えるのに使う。"""
    hold = {t.id: t.content_hash() for t in tasks if t.split == "holdout"}
    return {"at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
            "tasks": dict(sorted(hold.items())),
            "profiles": {p.id: {"harness": p.harness_hash(), "install_empty": not p.install} for p in sorted(profiles, key=lambda p: p.id)}}


def cli_version(adapter) -> str | None:
    try:
        r = subprocess.run(adapter.version_cmd(), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        line = (r.stdout or r.stderr).strip().splitlines()
        return line[0].strip() if r.returncode == 0 and line else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def append_spend(ledger: Path, usd: float | None, run_id: str, agent: str, billing: str) -> None:
    """費用台帳に 1 行追記する（月の予算の計算に使う）。agent は口座（claude_code / codex）、billing は api / subscription。"""
    if not usd:
        return
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "usd": usd, "run_id": run_id,
                            "agent": agent, "billing": billing}) + "\n")


def month_spent(ledger: Path, agent: str) -> float:
    """費用台帳（--out/spend.jsonl）から、その口座の今月（ローカル時刻）の実績を合計する。
    台帳には、エージェントと LLM 判定の費用を発生のたびに追記している（再試行・中断・doctor・harvest の分も含む）。
    - Claude と Codex は別の口座（月額上限がそれぞれ別）。口座の記録がない旧形式の行は、どちらにも数える（安全側）
    - サブスクリプション（billing: subscription）の行は API 単価換算の推定額なので数えない
    - 費用が分からない呼び出し（pricing なしの Codex など）は含められない"""
    now = datetime.now().astimezone()
    total = 0.0
    if Path(ledger).is_file():
        for line in Path(ledger).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                t = datetime.fromisoformat(r["at"]).astimezone()
            except (ValueError, KeyError, TypeError):
                continue
            if (t.year, t.month) != (now.year, now.month) or r.get("billing", "api") == "subscription":
                continue
            if r.get("agent") in (None, agent):
                total += float(r.get("usd") or 0)
    return total


def uses_llm(task: Task) -> bool:
    return any(g.type == "llm" for g in task.graders)


def cache_key(task: Task, profile: Profile, version: str | None, trial: int, opt: RunOptions, route: str = "", *,
              routes: str | None = None) -> str:
    """route: 試行に渡る経路の変数の要約（route_signature: 名前と、秘密でないものは値のハッシュ）。経路を変えれば再実行する。"""
    route = routes if routes is not None else route
    # 判定役の設定は入れない: LLM 判定は試行のあとに判定キャッシュ（判定入力 + モデル + ルーブリック + 段階のハッシュ）で行うので、
    # 段階や判定モデルを変えても解く側を再実行しない。ツールの版は入れる（起動コマンドや採点の作りが変われば再実行する）
    # LLM 判定の有無（--no-llm）は入れる: --no-llm では判定入力が記録に残らないため、同じ結果を LLM 判定ありの実行で再利用すると
    # 必須の LLM 判定を飛ばして採用できてしまう。段階・判定モデルは入れない（判定キャッシュで行うので）
    raw = "|".join([CACHE_SCHEMA, __version__, task.id, task.content_hash(), profile.id, profile.content_hash(), str(version), str(trial),
                    route, "llm" if opt.llm else "no-llm"])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class Runner(JudgePhase):
    def __init__(self, tasks: list[Task], profiles: list[Profile], opt: RunOptions, out_dir: Path):
        self.opt = opt
        reject_in_place(profiles)  # credentials: in_place は判定役専用。解く側に本物の設定ディレクトリを見せない
        self.pairs = select_pairs(tasks, profiles, opt)
        mixing = split_mixing(list({t.id: t for t, _ in self.pairs}.values()))
        if mixing:
            raise SpecError(mixing)
        # holdout はキャッシュを読まず書かない: 候補と現行を同じ時点で実行して比べるため（キャッシュの現行は別の時点の結果）、
        # また holdout の試行の記録（最終応答・ツール入力）を、train と共有するキャッシュフォルダに残さないため
        self.holdout = any(t.split == "holdout" for t, _ in self.pairs)
        if self.holdout:
            opt.use_cache = False
        self.out_dir = Path(out_dir).resolve()
        # 時刻順に並ぶ名前（同じ秒内の実行でも順序が崩れないようにミリ秒まで）
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + f"-{random.randint(0, 0xff):02x}"
        # 絶対パスにする（エージェントは作業ディレクトリを cwd に起動するため、相対パスだと HOME などがずれる）
        self.run_dir = (Path(out_dir) / self.run_id).resolve()
        opt.cache_dir = Path(opt.cache_dir).resolve()
        self.lock = threading.Lock()
        self.spent = 0.0
        self.versions: dict[str, str | None] = {}
        for _, p in self.pairs:
            if p.id not in self.versions:
                self.versions[p.id] = cli_version(get_adapter(p.agent, p))
        self.routes = {p.id: route_signature(p, dict(os.environ)) for _, p in self.pairs}
        self._identities: dict[tuple[str, str | None], str] = {}
        self._judge_seq = 0
        self._inplace_locks = {f: threading.Lock() for f in FAMILIES}  # credentials: in_place の判定は同じ系統で同時に 1 本だけ
        self.interrupted = threading.Event()
        self.ledger = Path(opt.ledger or Path(out_dir) / "spend.jsonl").resolve()
        self.needs_judge = opt.llm and any(uses_llm(t) or t.calibration for t, _ in self.pairs)
        self.judge: JudgeSettings | None = None
        self.judge_profiles: dict[str, Profile | None] = {}
        self.judge_versions: dict[str, str | None] = {}
        if self.needs_judge:
            fams = [opt.judge_agent] if opt.judge_agent else list(opt.judge_families or FAMILIES)
            for f in fams:
                self.judge_profiles[f] = self._pick_judge_profile(profiles, f)
                self.judge_versions[f] = cli_version(get_adapter(f, self.judge_profiles[f], executable=self._judge_exe(f)))
            available = [f for f, v in self.judge_versions.items() if v is not None]
            self.judge = make_settings(opt.tier, {p.agent for _, p in self.pairs}, available, opt.judge_agent, opt.judge_models,
                                       opt.judge_votes, opt.allow_weak_judge, opt.seed, opt.calibration_items)
        # 月の予算: 口座（エージェント）ごとに、台帳の今月の実績を引いた残りをこの実行の上限にする
        self.month_spent: dict[str, float] | None = None
        self.spent_api: dict[str, float] = defaultdict(float)  # この実行で口座に数える支出（billing: api のみ）
        if opt.monthly_budget_usd is not None:
            agents = {p.agent for _, p in self.pairs} | set(self.judge.used_families() if self.judge else [])
            self.month_spent = {a: month_spent(self.ledger, a) for a in sorted(agents)}
        self.judge_usage = empty_usage()
        self.retry_usage = empty_usage()  # 再試行で捨てた解く側の呼び出しの使量（最後の試行の記録だけでは数えられない）
        self.judge_usage.update({"cache_hits": 0, "by_model": {}})
        self.calibration: dict[str, Any] | None = None
        missing = [a for a, p in (self.judge.plan if self.judge else {}).items() if not p["families"]]
        warn = []
        if missing:
            warn.append(f"判定役の CLI が見つかりません（評価対象 {missing}）。LLM 判定のある試行は judge_error として除外されます")
        same = [a for a, p in (self.judge.plan if self.judge else {}).items() if p["same_family"]]
        if same:
            warn.append(f"同系統の判定役（評価対象 {same}）。別系統の CLI がないため同系統で判定します。"
                        "LLM 判定が合否を決めた試行があれば結論は INCONCLUSIVE になります")
        if self.judge and self.judge.weak():
            warn.append(f"弱い判定役（{', '.join(self.judge.weak())}。--allow-weak-judge）。結論は採用判断に使えません")
        no_auth = [f for f in (self.judge.used_families() if self.judge else []) if self.judge_profiles.get(f) is None]
        if no_auth:
            warn.append(f"LLM 判定（{no_auth}）の認証を借りる同種のプロファイルがありません。親の環境変数だけで判定します")
        self.judge_warning = "\n注意: ".join(warn) or None

    def _judge_exe(self, family: str) -> list[str] | None:
        if family in self.opt.judge_executables:
            return self.opt.judge_executables[family]
        if self.opt.judge_executable and family == (self.opt.judge_agent or "claude_code"):
            return self.opt.judge_executable
        return None

    def _spend(self, usd: float | None, agent: str, billing: str) -> None:
        """費用を実行内の合計・口座別の合計・台帳に記録する。--max-cost-usd は推定額の合計で止める（サブスクリプションも含む。
        実行の大きさの上限として使うため）。月の予算は billing: api の支出だけを数える。"""
        if not usd:
            return
        with self.lock:
            self.spent += usd
            if billing == "api":
                self.spent_api[agent] += usd
            append_spend(self.ledger, usd, self.run_id, agent, billing)

    @staticmethod
    def _settle_cost(rec: TrialRecord, profile: Profile) -> tuple[float | None, str | None]:
        """1 回の呼び出しの費用: 公開価格で換算できればそれ（price_table）、できなければ CLI の推定、どちらもなければ不明のまま。"""
        cost, src = solver_cost(rec, profile.model)
        if src == "price_table":
            return cost, "price_table"
        return rec.cost_usd, rec.cost_source

    def month_remaining(self, agent: str) -> float | None:
        if self.month_spent is None or self.opt.monthly_budget_usd is None:
            return None
        return max(self.opt.monthly_budget_usd - self.month_spent.get(agent, 0.0), 0.0)

    def _over_budget(self, task: Task, profile: Profile) -> bool:
        with self.lock:
            if self.opt.max_cost_usd is not None and self.spent >= self.opt.max_cost_usd:
                return True
            accounts = [profile.agent] if profile.billing == "api" else []
            if self.judge and uses_llm(task):
                accounts += [f for f in self.judge.families_for(profile.agent) if self._judge_billing(f) == "api"]
            return any((rem := self.month_remaining(a)) is not None and self.spent_api[a] >= rem for a in accounts)

    def _record(self, task: Task, profile: Profile, trial: int, key: str, **kw: Any) -> TrialRecord:
        return TrialRecord(run_id=self.run_id, task_id=task.id, profile_id=profile.id, agent=profile.agent,
                           trial=trial, cache_key=key, task_hash=task.content_hash(), profile_hash=profile.content_hash(),
                           cli_version=self.versions.get(profile.id), **kw)

    def _judge_billing(self, family: str) -> str:
        jp = self.judge_profiles.get(family)
        return jp.billing if jp else "api"

    def judge_route_profiles(self) -> list[Profile]:
        """LLM 判定が使う認証の出どころ（借りるプロファイル。なければ親の環境だけの仮のプロファイル）。経路の確認に使う。"""
        return [self.judge_profiles.get(f) or Profile(id=f"LLM 判定（{f}・親の環境）", agent=f, path=self.run_dir / "judge.yaml",
                                                      inherit_env=list(ROUTE_VARS[f]))
                for f in (self.judge.used_families() if self.judge else [])]

    def _judge_refs(self) -> list[tuple[str | None, str]]:
        """--judge-profile の指定を (系統 or None, 参照) にする。`codex=参照` は系統つき、`参照` だけなら参照先の系統。"""
        refs = self.opt.judge_profile
        refs = [refs] if isinstance(refs, str) else list(refs or [])
        out: list[tuple[str | None, str]] = []
        for r in refs:
            fam, sep, ref = r.partition("=")
            out.append((fam, ref) if sep and fam in FAMILIES else (None, r))
        return out

    @staticmethod
    def _load_judge_ref(profiles: list[Profile], ref: str) -> Profile:
        """参照は、実行のプロファイルの id か、実行のプロファイルにない YAML のパス（判定役の認証だけ借りる。配布物は使わない）。"""
        hit = [p for p in profiles if p.id == ref]
        if hit:
            return hit[0]
        if Path(ref).is_file():
            return load_profile(Path(ref))
        raise SpecError(f"--judge-profile '{ref}' は、実行のプロファイルの id でも YAML のパスでもありません")

    def _pick_judge_profile(self, profiles: list[Profile], family: str) -> Profile | None:
        """LLM 判定の認証（env・env_unset・copy_credentials / credentials・executable）を借りるプロファイル。
        指定がなければ、判定の系統と同じ種類の基準プロファイル → 最初の同種プロファイルの順。配布物（install）は使わない。"""
        mismatch = None
        for fam, ref in self._judge_refs():
            p = self._load_judge_ref(profiles, ref)
            if fam is not None and p.agent != fam:
                raise SpecError(f"--judge-profile '{fam}={ref}' は {p.agent} 用のプロファイルです（系統 {fam} ではありません）")
            if p.agent == family:
                return p
            mismatch = mismatch or (ref, p.agent)
        if mismatch and self.opt.judge_agent:
            raise SpecError(f"--judge-profile '{mismatch[0]}' は {mismatch[1]} 用です（--judge-agent {self.opt.judge_agent}）")
        same = [p for p in profiles if p.agent == family]
        base = [p for p in same if p.id == self.opt.baseline]
        return (base or same or [None])[0]

    # ------------------------------------------------------------ plan
    def plan(self) -> dict[str, Any]:
        """実行前の見積もり（何回エージェントを呼ぶか、キャッシュで何回省けるか、解く側と判定側の概算費用を段階ごとに）。"""
        hist = self._cost_history()
        rows, calls, hits, est, unknown = [], 0, 0, 0.0, 0
        for t, p in self.pairs:
            for i in range(self.opt.trials):
                key = cache_key(t, p, self.versions[p.id], i, self.opt, routes=self.routes[p.id])
                if self.opt.use_cache and self._cache_get(key):
                    hits += 1
                    continue
                calls += 1
                c = hist.get((p.id, t.id)) or hist.get((p.id, None))
                if c is None:  # 履歴がなくても $0 とは数えない（開始前の --max-cost-usd の判定をすり抜けないため）
                    unknown += 1
                    c = solver_call_estimate(t.max_budget_usd)
                est += c
            rows.append((t.id, p.id))
        return {"pairs": len(rows), "trials_total": len(rows) * self.opt.trials, "agent_calls": calls,
                "cache_hits": hits, "estimated_cost_usd": est, "unknown_cost_calls": unknown,
                "solver": {"estimated_cost_usd": est, "unknown_cost_calls": unknown, "calls": calls},
                "judge": self.judge_estimate() if self.needs_judge else None,
                "llm_judge": self.needs_judge, "cli_versions": self.versions}

    def estimated_total(self, plan: dict[str, Any]) -> float:
        j = (plan.get("judge") or {}).get(self.judge.tier if self.judge else "", {})
        return plan["solver"]["estimated_cost_usd"] + (j.get("estimated_cost_usd_max") or 0.0)

    def _cost_history(self) -> dict[tuple[str, str | None], float]:
        acc: dict[tuple[str, str | None], list[float]] = {}
        d = self.opt.cache_dir
        if d.is_dir():
            for f in d.glob("*.json"):
                try:
                    r = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if r.get("cost_usd") is None:
                    continue
                c = r["cost_usd"] + (r.get("judge_cost_usd") or 0)
                acc.setdefault((r["profile_id"], r["task_id"]), []).append(c)
                acc.setdefault((r["profile_id"], None), []).append(c)
        return {k: sum(v) / len(v) for k, v in acc.items()}

    # ------------------------------------------------------------ cache
    def _cache_get(self, key: str) -> TrialRecord | None:
        f = self.opt.cache_dir / f"{key}.json"
        if not f.is_file():
            return None
        try:
            return TrialRecord.from_dict(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def _cache_put(self, rec: TrialRecord) -> None:
        # 再利用してよいのは有効な結果だけ。タイムアウトはネットワーク断などの一時的な原因でも起きるため保存しない
        if not self.opt.use_cache or not rec.valid or (rec.error or {}).get("type") == "timeout":
            return
        self.opt.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.opt.cache_dir / f"{rec.cache_key}.json").write_text(
            json.dumps(rec.to_dict(), ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------ trial
    def _execute(self, task: Task, profile: Profile, trial: int, key: str) -> TrialRecord:
        adapter = get_adapter(profile.agent, profile)
        tdir = self.run_dir / "trials" / task.id / profile.id / str(trial)
        tp = make_trial_paths(tdir, adapter.config_dirname)
        try:
            return self._execute_in(task, profile, trial, key, adapter, tdir, tp)
        finally:
            # 一時ホームは認証情報の複製を含みうるので、例外・中断（Ctrl-C）でも必ず消す
            release_home(tp, self.opt.log)

    def _execute_in(self, task: Task, profile: Profile, trial: int, key: str, adapter, tdir: Path, tp) -> TrialRecord:
        rec = self._record(task, profile, trial, key, trial_dir=str(tdir), started_at=datetime.now(timezone.utc).isoformat())
        if self.interrupted.is_set():  # 準備の前に Ctrl-C された → 何もしない
            rec.valid, rec.error = False, {"type": "interrupted", "message": "Ctrl-C で中断"}
            return rec
        try:
            if task.fixture:
                if task.fixture.is_dir():
                    shutil.copytree(task.fixture, tp.workdir, dirs_exist_ok=True)
                else:
                    shutil.copy2(task.fixture, tp.workdir / task.fixture.name)
            env = build_env(profile, tp, adapter.config_env)
            if profile.copy_credentials:
                copy_credentials(profile.agent, tp)
            run_install(profile, tp, env)
        except Exception as e:
            rec.error = {"type": "harness_error", "message": str(e)[:1000]}
            rec.valid = False
            return rec
        before = snapshot(tp.workdir)
        tests_before = capture_tests(tp.workdir)  # 改ざんの基準（エージェントを起動する直前の作業ツリー）
        ph = placeholders(tp, profile)
        cmd = adapter.build_cmd(task, profile, tp.workdir, [fill(a, ph) for a in profile.extra_args])
        if sandbox_mode(profile) == "bwrap":
            cmd = sandbox_cmd(cmd, tp, env, profile, n_exe=len(adapter.exe))
        if self.interrupted.is_set():  # 準備（install など）の間に Ctrl-C された → エージェントを起動しない
            rec.valid, rec.error = False, {"type": "interrupted", "message": "Ctrl-C で中断"}
            return rec
        pr = run_process(cmd, tp.workdir, env, task.prompt, tp.stdout, tp.stderr, task.timeout_s)
        rec.wall_ms = pr.wall_ms
        out_lines = tp.stdout.read_text(encoding="utf-8", errors="replace").splitlines() if tp.stdout.exists() else []
        stderr = tp.stderr.read_text(encoding="utf-8", errors="replace") if tp.stderr.exists() else ""
        parsed = adapter.parse(out_lines, profile)
        etype, emsg = parsed.error_type, parsed.error_message
        if pr.launch_error:
            etype, emsg = "launch_failed", pr.launch_error
        elif parsed.meta.get("network_stalled") and (pr.timed_out or etype == "agent_crash"):
            etype, emsg = "network", "API に接続できず再接続を繰り返した（環境起因として除外）"
        elif pr.timed_out:
            etype, emsg = "timeout", f"{task.timeout_s} 秒でタイムアウト"
        elif etype == "agent_crash" or (etype is None and pr.returncode not in (0, None)):
            etype = classify_stderr(stderr) or "agent_crash"
            emsg = (emsg or "") + f" exit={pr.returncode} " + stderr.strip()[-300:]
        after = snapshot(tp.workdir)
        rec.files_created = sorted(k for k in after if k not in before)
        rec.files_modified = sorted(k for k in after if k in before and after[k] != before[k])
        rec.tool_calls = [vars(c) for c in parsed.tool_calls]
        rec.skill_calls, rec.subagent_calls = parsed.skill_calls, parsed.subagent_calls
        rec.hook_calls = parsed.hook_calls if task.uses_hooks else None  # 記録を取ったときだけ（取っていなければ不明）
        rec.final_message, rec.cost_usd, rec.cost_source = parsed.final_message, parsed.cost_usd, parsed.cost_source
        rec.duration_ms = parsed.duration_ms or pr.wall_ms
        rec.num_turns, rec.tokens = parsed.num_turns, parsed.tokens
        rec.meta, rec.warnings, rec.skill_detection = parsed.meta, parsed.warnings, parsed.skill_detection
        rec.meta["sandbox"] = sandbox_mode(profile)
        leak = leak_check(rec.tool_calls, "\n".join(out_lines), tdir, self._protected(task), self._sandbox_allow(profile, tp),
                          task.canaries, task.leak_patterns)
        if leak["warnings"]:
            rec.meta["leak_warnings"] = leak["warnings"][:5]
        if leak["hits"]:  # 答えを見た・見ようとした → 合格にも不合格にも数えない（他のエラーより優先）
            rec.meta["leak"] = leak["hits"][:10]
            etype, emsg = "leak", "評価の答え・試行の外を参照: " + summarize_hits(leak)
        if etype:
            rec.error = {"type": etype, "message": (emsg or "")[:1000]}
        rec.valid = etype not in EXCLUDED_ERRORS
        rec.invalid_reason = None if rec.valid else etype
        if rec.valid:
            tamper = [] if task.allow_test_changes else tamper_check(tests_before, tp.workdir)
            if tamper:
                rec.meta["tamper"] = tamper[:10]
            # 決定的な検査を先に行い、LLM 判定は合格した出力の判定入力（最小）だけを積んでおく。判定は全試行のあと（_judge_phase）。
            # 採点の一時ファイルは試行ごとのフォルダに集め、終わったら消す（隠しテストを当てた木を OS の /tmp に残さない）
            gtmp = fresh_dir(tp.root / "grade_tmp")  # solver が同名のリンクを置いていても、その先を使わない
            try:
                grade_all(task.graders, rec, tp.workdir, JudgeConfig(enabled=self.opt.llm, run=None, defer=True), task.path.parent,
                          tmp_dir=gtmp, precheck=tamper_result(tamper) if tamper else None)
            finally:
                shutil.rmtree(gtmp, ignore_errors=True)
            if task.calibration and rec.error is None and not tamper:
                c = task.calibration
                content = judge_content(str(c.get("focus", "final")), int(c.get("max_chars", 20000)), rec, tp.workdir)
                picked = [rec.grader_results[i] for i in c["graders"]] if c.get("graders") else rec.grader_results
                code = [g for g in picked if g["type"] != "llm" and not g["harness_only"] and g["status"] != "not_run"]
                if content is not None and code:
                    rec.judge_inputs.append({"kind": "calibration", "criteria": str(c["criteria"]), "focus": str(c.get("focus", "final")),
                                             "content": content, "answer": all(g["status"] == "pass" for g in code)})
            self._finalize(rec, etype)
        (tp.root / "record_for_grader.json").unlink(missing_ok=True)
        return rec

    def _protected(self, task: Task) -> list[Path]:
        """leak 検査で守る場所: 本物のホーム・--out・キャッシュ・AHE 自身・タスクのフォルダ・フィクスチャの元・採点スクリプト、
        とそれらの共通の親（examples/ のように参照解・採点が並ぶ評価の置き場所）。"""
        mats = [task.path.parent] + ([task.fixture] if task.fixture else [])
        for g in task.graders:
            if g.type == "command":
                run = str(g.params.get("run", "")).replace("{task_dir}", str(task.path.parent))
                mats += [Path(t).resolve().parent for t in re.findall(r"[\w./\\:-]+\.(?:py|sh|ps1|js)", run)
                         if Path(t).is_absolute() and Path(t).exists()]
        try:
            common = Path(os.path.commonpath([str(m) for m in mats]))
            if len(common.parts) >= 3 and common != Path.home():
                mats.append(common)
        except ValueError:  # 別のドライブ
            pass
        return [Path.home(), self.out_dir, self.opt.cache_dir, Path(__file__).resolve().parents[1], *mats]

    @staticmethod
    def _sandbox_allow(profile: Profile, tp) -> list[Path]:
        ph = placeholders(tp, profile)
        return [Path(fill(x, ph)) for x in profile.sandbox_ro]

    def _finalize(self, rec: TrialRecord, etype: str | None) -> None:
        """grader_results から合否を決める。判定待ち（pending）が残っていれば、まだ決めない。"""
        rescore(rec)
        if any(g["status"] == "pending" for g in rec.grader_results):
            rec.passed, rec.cmp_passed = False, None
            return
        if rec.valid and rec.score is None and rec.error is None:  # 採点できるグレーダーが 1 つもない（例: llm だけのタスクを --no-llm）
            rec.error, rec.valid = {"type": "not_graded", "message": "実行されたグレーダーがありません"}, False
        rec.passed = rec.valid and rec.error is None and rec.score == 1.0
        rec.cmp_passed = (rec.valid and rec.error is None and rec.cmp_score == 1.0) if rec.cmp_score is not None else None
        rec.failure_tags = []
        if rec.valid and not rec.passed:
            tags = [etype] if etype else []
            tags += [g["category"] for g in rec.grader_results if g["status"] in ("fail", "unknown", "judge_disagreement")]
            rec.failure_tags = list(dict.fromkeys(tags))

    def _cleanup(self, rec: TrialRecord) -> None:
        if not rec.trial_dir:
            return
        root = Path(rec.trial_dir)
        if self.opt.keep == "none" or (self.opt.keep == "failed" and rec.passed):
            shutil.rmtree(root / "workdir", ignore_errors=True)
        if self.opt.keep == "none":  # 生ログ（トランスクリプト）も残さない
            for name in ("stdout.jsonl", "stderr.txt"):
                (root / name).unlink(missing_ok=True)

    def _one(self, task: Task, profile: Profile, trial: int, sink) -> TrialRecord:
        key = cache_key(task, profile, self.versions[profile.id], trial, self.opt, routes=self.routes[profile.id])
        if self.opt.use_cache:
            hit = self._cache_get(key)
            if hit:
                hit.from_cache, hit.run_id = True, self.run_id
                sink(hit)
                return hit
        attempt = 0
        while True:
            if self.interrupted.is_set():  # 再試行待ちの間に Ctrl-C された → 新しく呼ばない
                rec = self._record(task, profile, trial, key, valid=False, error={"type": "interrupted", "message": "Ctrl-C で中断"})
                sink(rec)
                return rec
            if self._over_budget(task, profile):
                rec = self._record(task, profile, trial, key, valid=False,
                                   error={"type": "budget_stop", "message": "費用の上限（--max-cost-usd / --monthly-budget-usd）に到達したため未実行"})
                sink(rec)
                return rec
            rec = self._execute(task, profile, trial, key)
            # 呼び出しごとに費用を 1 度だけ確定し、予算・台帳・集計（usage）に同じ額を渡す（pricing のない Codex 試行も公開価格で換算）
            rec.cost_usd, rec.cost_source = self._settle_cost(rec, profile)
            self._spend(rec.cost_usd, profile.agent, profile.billing)
            if self.interrupted.is_set():  # Ctrl-C で止めた試行は結果ではない（失敗として数えず、キャッシュもしない）
                rec.valid, rec.passed, rec.score, rec.failure_tags = False, False, None, []
                rec.error = {"type": "interrupted", "message": "Ctrl-C で中断"}
            retryable = not rec.valid and (rec.error or {}).get("type") in ("rate_limit", "overloaded")
            if retryable and attempt < self.opt.retry_infra:
                attempt += 1
                with self.lock:
                    add_usage(self.retry_usage, rec.tokens, rec.cost_usd)
                self.opt.log(f"  再試行 {attempt}/{self.opt.retry_infra}: {task.id}/{profile.id}#{trial} ({rec.error})")
                self.interrupted.wait(self.opt.retry_wait_s * attempt)  # Ctrl-C ですぐ抜ける
                continue
            break
        self._cleanup(rec)
        self._cache_put(rec)
        sink(rec)
        return rec

    def preflight(self) -> None:
        """試行を 1 つも始める前の確認。隔離（bwrap）が使えない・複製する認証の期限が近いなら止める（SpecError）。"""
        profiles = list({p.id: p for _, p in self.pairs}.values())
        boxed = [p.id for p in profiles if sandbox_mode(p) == "bwrap"]
        problem = bwrap_problem() if boxed else None
        if problem:
            raise SpecError(f"{problem}。試行の隔離（{boxed} の sandbox: bwrap。既定）ができません。bwrap を入れるか、"
                            "隔離しない場合はプロファイルに sandbox: off と書いてください（結果に記録され、verdict が警告します）")
        lenders = [p for p in profiles + [x for x in self.judge_profiles.values() if x] if p.copy_credentials]
        for agent in sorted({p.agent for p in lenders}):
            msg = credentials.preflight(agent)
            if msg:
                raise SpecError(msg)
        in_place = sorted({p.agent for p in self.judge_profiles.values() if p and p.credentials == "in_place"})
        if in_place and not self.opt.trust_judge_input:
            raise SpecError(
                f"判定役の認証（credentials: in_place、{', '.join(in_place)}）は、判定 CLI が実の認証ディレクトリを指したまま動きます。"
                "CLI のツール（ファイルの読み取りなど）がそこを読めないよう止める手段を、ハーネスは持てません。判定入力に候補の出力が入るため、"
                "出力に埋め込まれた指示で認証の中身が判定の理由・記録に出うる限界があります。判定入力（解く側の出力）が信頼できる"
                "（自分で用意した合成データ・自分のエージェントの出力）と承知したうえで --trust-judge-input を付けてください。"
                "信頼できない出力を判定するなら、API キー（inherit_env）か copy_credentials を使ってください")
        for agent in in_place:
            src = credentials.source_path(agent)
            if src is None or not src.is_file():  # 中身は読まない・出さない（あるかどうかだけ）
                raise SpecError(f"判定役の認証（credentials: in_place）の {agent} の認証ファイル {src} がありません。先に `{agent} login` してください")
            msg = credentials.preflight(agent, in_place=True)
            if msg:
                raise SpecError(msg)

    def run(self) -> tuple[Path, list[TrialRecord], dict[str, Any]]:
        self.preflight()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc).isoformat()
        records: list[TrialRecord] = []
        if self.holdout:
            # holdout を見た記録は実行の前に書く（中断しても、途中までのトランスクリプトで holdout を見たことに変わりはない）
            row = holdout_row(self.run_id, list({t.id: t for t, _ in self.pairs}.values()), list({p.id: p for _, p in self.pairs}.values()))
            with (self.out_dir / HOLDOUT_LEDGER).open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if self.opt.prereg is not None:
            # 事前登録: 判定規則と判定役の設定を、試行を 1 つも実行する前に固定して保存する（ハッシュ付き）
            (self.run_dir / "prereg.json").write_text(json.dumps(prereg_record(self.opt.prereg, self.judge), ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
        jsonl = (self.run_dir / "trials.jsonl").open("w", encoding="utf-8")

        def sink(rec: TrialRecord) -> None:
            with self.lock:
                records.append(rec)
                jsonl.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
                jsonl.flush()
                mark = "CACHE" if rec.from_cache else ("PASS" if rec.passed else ("SKIP" if not rec.valid else "FAIL"))
                cost = f"${rec.cost_usd:.4f}" if rec.cost_usd is not None else "$?"
                err = f" [{rec.error['type']}]" if rec.error else ""
                self.opt.log(f"[{len(records)}/{len(self.pairs) * self.opt.trials}] {mark:5} {rec.task_id} / {rec.profile_id} #{rec.trial} score={rec.score if rec.score is None else round(rec.score, 2)} {cost}{err}")

        jobs = schedule(self.pairs, self.opt.trials, self.opt.seed)
        if self.judge_warning:
            self.opt.log(f"注意: {self.judge_warning}")
        ex = ThreadPoolExecutor(max_workers=max(1, self.opt.jobs))
        try:
            for f in [ex.submit(self._one, t, p, i, sink) for t, p, i in jobs]:
                f.result()
        except KeyboardInterrupt:
            # 中断: 未開始の試行は取り消し、実行中のエージェントは止める（トークンを使い続けない）。
            # 実行中だった試行は後始末（一時ホームの削除）まで待つ
            self.opt.log("中断します: 未開始の試行を取り消し、実行中のエージェントを停止しています…")
            self.interrupted.set()
            ex.shutdown(wait=False, cancel_futures=True)
            kill_running()
            raise
        finally:
            ex.shutdown(wait=True)
            jsonl.close()
        try:
            self._judge_phase(records)
        except KeyboardInterrupt:
            self.interrupted.set()
            kill_running()
            raise
        finally:
            shutil.rmtree(self.run_dir / "judge", ignore_errors=True)
        for r in records:
            if r.judge_votes and not r.from_cache:  # 判定で合格が決まった試行の作業フォルダ（キャッシュの試行は別の実行のもの）
                self._cleanup(r)
        records.sort(key=lambda r: (r.profile_id, r.task_id, r.trial))
        # 判定のあとの最終の記録で書き直す（実行中は判定待ちのまま追記している）
        (self.run_dir / "trials.jsonl").write_text("".join(json.dumps(r.to_dict(), ensure_ascii=False) + "\n" for r in records),
                                                   encoding="utf-8")
        summary = summarize([r for r in records if (r.error or {}).get("type") not in NOT_RUN_ERRORS], self.opt.kk)
        meta = {
            "run_id": self.run_id, "tool_version": __version__, "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "platform": f"{platform.system()} {platform.release()}", "python": sys.version.split()[0],
            "options": {"trials": self.opt.trials, "k": self.opt.kk, "tags": self.opt.tags, "splits": self.opt.splits, "llm": self.opt.llm,
                        "tier": self.opt.tier, "judge_agent": self.opt.judge_agent, "judge_models": self.opt.judge_models,
                        "judge_votes": self.opt.judge_votes, "allow_weak_judge": self.opt.allow_weak_judge,
                        "judge_profiles": {f: (p.id if p else None) for f, p in self.judge_profiles.items()},
                        "judge_credentials": {f: ("in_place" if p and p.credentials == "in_place" else "copy" if p and p.copy_credentials else "env")
                                              for f, p in self.judge_profiles.items()},
                        "judge_input_trusted": self.opt.trust_judge_input,
                        "jobs": self.opt.jobs,
                        "max_cost_usd": self.opt.max_cost_usd, "monthly_budget_usd": self.opt.monthly_budget_usd,
                        "month_spent_before_run_usd": self.month_spent, "use_cache": self.opt.use_cache,
                        "baseline": self.opt.baseline, "seed": self.opt.seed},
            "cli_versions": self.versions,
            "profiles": {p.id: {"agent": p.agent, "hash": p.content_hash(), "model": p.model, "path": str(p.path),
                                "harness_hash": p.harness_hash(), "install_empty": not p.install}
                         for p in {p.id: p for _, p in self.pairs}.values()},
            "tasks": {t.id: {"hash": t.content_hash(), "tags": t.tags, "paired_negative": t.paired_negative, "llm": uses_llm(t),
                             "split": t.split, "family": t.family}
                      for t in {t.id: t for t, _ in self.pairs}.values()},
            "spent_usd_this_run": self.spent,
            "judge": (self.judge.meta(self.calibration, {k: v for k, v in self.judge_usage.items()})
                      | {"cli_versions": self.judge_versions}) if self.judge else
                     {"llm": False, "tier": self.opt.tier, "note": "LLM 判定なし（--no-llm か、LLM 判定のタスクがない）",
                      "calibration": {"needed": False, "items": 0, "rate": None}},
            "ancestor_instructions": ancestor_instructions(self.run_dir.parent),
            "not_run": sum(1 for r in records if (r.error or {}).get("type") in NOT_RUN_ERRORS),
        }
        summary["meta"] = meta
        summary["usage"] = usage_summary(records, self.judge_usage, {p.id: p.model for _, p in self.pairs},
                                         self.judge.tier if self.judge else None, self.retry_usage)
        (self.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.run_dir, records, summary


def prereg_record(rules: dict[str, Any], judge: JudgeSettings | None) -> dict[str, Any]:
    """事前登録の中身。rules_hash は規則だけのハッシュ（verdict が照合する）。"""
    from .soundness import rules_hash
    return {"registered_at": datetime.now(timezone.utc).isoformat(), "tool_version": __version__, "rules": rules,
            "rules_hash": rules_hash(rules),
            "judge": judge.meta() if judge else {"llm": False}}


def load_run(run_dir: Path) -> tuple[list[TrialRecord], dict[str, Any]]:
    if not (run_dir / "summary.json").is_file() or not (run_dir / "trials.jsonl").is_file():
        raise SpecError(f"{run_dir}: 実行結果（summary.json・trials.jsonl）がありません")
    recs = [TrialRecord.from_dict(json.loads(l)) for l in
            (run_dir / "trials.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    return recs, summary
