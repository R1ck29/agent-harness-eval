"""実行の司令塔: タスク × プロファイル × 試行回数 を回し、記録・採点・集計する。

トークン節約の仕組み（評価の正しさを損なわないものだけ）:
- キャッシュ: タスク・フィクスチャ・グレーダー・プロファイル・配布物・CLI 版・試行番号・判定設定が
  すべて同じ試行は、過去の有効な結果を再利用する（変更していない組み合わせは再実行しない）
- LLM 判定は、コード判定がすべて合格した試行にだけ実行する（合否は変わらない）
- 試行ごと（max_turns / max_budget_usd / timeout_s）と実行全体（--max-cost-usd）に上限
- 異常終了（レート制限など）だけ再試行する。失敗した試行を「良い結果が出るまで」再実行はしない
"""
from __future__ import annotations

import hashlib
import json
import platform
import random
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .adapters import classify_stderr, get_adapter
from .graders import JudgeConfig, JudgeUnavailable, grade_all
from .isolation import (build_env, copy_credentials, fill, kill_running, make_trial_paths, placeholders,
                        run_install, run_process, snapshot)
from .metrics import summarize
from .record import EXCLUDED_ERRORS, TrialRecord
from .spec import Profile, SpecError, Task

NOT_RUN_ERRORS = {"budget_stop"}
CACHE_SCHEMA = "4"  # 試行レコードの形式を変えたら上げる（古いキャッシュを使わないため）


@dataclass
class RunOptions:
    trials: int = 1
    k: int | None = None
    tags: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    jobs: int = 1
    llm: bool = True
    judge_agent: str = "claude_code"
    judge_model: str | None = "haiku"
    judge_executable: list[str] | None = None
    max_cost_usd: float | None = None
    monthly_budget_usd: float | None = None
    ledger: Path | None = None  # 費用台帳（既定: 出力先/spend.jsonl）
    retry_infra: int = 1
    retry_wait_s: float = 20.0
    judge_profile: str | None = None  # 判定の認証・環境変数を借りるプロファイル id（既定: 自動選択）
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
            out.append((t, p))
    return out


def schedule(pairs: list[tuple[Task, Profile]], trials: int, seed: int) -> list[tuple[Task, Profile, int]]:
    """実行順。同じ（試行, タスク）の各プロファイルを続けて実行し、先に走るプロファイルをブロックごとに巡回させる。
    プロファイルをまとめて順に回すと、時間とともに変わる条件（API の混雑・モデルの更新）が差に混ざるため。
    巡回の起点は seed で決める（同じ seed なら同じ順）。"""
    by_task: dict[str, list[tuple[Task, Profile]]] = {}
    for t, p in pairs:
        by_task.setdefault(t.id, []).append((t, p))
    order = sorted({p.id for _, p in pairs})
    random.Random(seed).shuffle(order)
    jobs: list[tuple[Task, Profile, int]] = []
    block = 0
    for i in range(trials):
        for items in by_task.values():
            items = sorted(items, key=lambda tp: order.index(tp[1].id))
            r = block % len(items)
            jobs += [(t, p, i) for t, p in items[r:] + items[:r]]
            block += 1
    return jobs


def cli_version(adapter) -> str | None:
    try:
        r = subprocess.run(adapter.version_cmd(), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        line = (r.stdout or r.stderr).strip().splitlines()
        return line[0].strip() if r.returncode == 0 and line else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def append_spend(ledger: Path, usd: float | None, run_id: str) -> None:
    """費用台帳に 1 行追記する（月の予算の計算に使う）。"""
    if not usd:
        return
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "usd": usd, "run_id": run_id}) + "\n")


def month_spent(ledger: Path) -> float:
    """費用台帳（--out/spend.jsonl）から今月（ローカル時刻）の実績を合計する。
    台帳には、エージェントと LLM 判定の費用を発生のたびに追記している（再試行・中断・doctor の分も含む）。
    費用が分からない呼び出し（pricing なしの Codex など）は含められない。"""
    now = datetime.now().astimezone()
    total = 0.0
    if Path(ledger).is_file():
        for line in Path(ledger).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                t = datetime.fromisoformat(r["at"]).astimezone()
            except (ValueError, KeyError, TypeError):
                continue
            if (t.year, t.month) == (now.year, now.month):
                total += float(r.get("usd") or 0)
    return total


def uses_llm(task: Task) -> bool:
    return any(g.type == "llm" for g in task.graders)


def cache_key(task: Task, profile: Profile, version: str | None, trial: int, opt: RunOptions, judge_sig: str = "") -> str:
    judge = f"{opt.llm}:{opt.judge_agent}:{opt.judge_model}:{judge_sig}" if uses_llm(task) else "-"
    raw = "|".join([CACHE_SCHEMA, task.id, task.content_hash(), profile.id, profile.content_hash(), str(version), str(trial), judge])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class Runner:
    def __init__(self, tasks: list[Task], profiles: list[Profile], opt: RunOptions, out_dir: Path):
        self.opt = opt
        self.pairs = select_pairs(tasks, profiles, opt)
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
        self._judge_seq = 0
        self.interrupted = threading.Event()
        self.month_spent: float | None = None
        self.ledger = Path(opt.ledger or Path(out_dir) / "spend.jsonl").resolve()
        if opt.monthly_budget_usd is not None:
            # 月の予算: 台帳の今月の実績を合計し、残りをこの実行の上限にする
            self.month_spent = month_spent(self.ledger)
            remaining = max(opt.monthly_budget_usd - self.month_spent, 0.0)
            opt.max_cost_usd = remaining if opt.max_cost_usd is None else min(opt.max_cost_usd, remaining)
        self.judge_profile = self._pick_judge_profile(profiles) if opt.llm else None
        jp = self.judge_profile
        # 判定の結果に効きうるもの（借りるプロファイルの環境変数・実行ファイル）をキャッシュキーに入れる
        self.judge_sig = hashlib.sha256(json.dumps(
            [jp.id if jp else None, jp.env if jp else None, jp.env_unset if jp else None,
             jp.executable if jp else None, opt.judge_executable], sort_keys=True).encode()).hexdigest()[:12]
        self.judge_warning = (f"LLM 判定（{opt.judge_agent}）の認証を借りる同種のプロファイルがありません。親の環境変数だけで判定します"
                              if opt.llm and jp is None and any(uses_llm(t) for t, _ in self.pairs) else None)

    def _spend(self, usd: float | None) -> None:
        """費用を実行内の合計と台帳の両方に記録する（月の予算の計算に使う）。"""
        if not usd:
            return
        with self.lock:
            self.spent += usd
            append_spend(self.ledger, usd, self.run_id)

    def _record(self, task: Task, profile: Profile, trial: int, key: str, **kw: Any) -> TrialRecord:
        return TrialRecord(run_id=self.run_id, task_id=task.id, profile_id=profile.id, agent=profile.agent,
                           trial=trial, cache_key=key, task_hash=task.content_hash(), profile_hash=profile.content_hash(),
                           cli_version=self.versions.get(profile.id), **kw)

    def _pick_judge_profile(self, profiles: list[Profile]) -> Profile | None:
        """LLM 判定の認証（env・env_unset・copy_credentials・executable）を借りるプロファイル。
        指定がなければ、判定エージェントと同じ種類の基準プロファイル → 最初の同種プロファイルの順。配布物（install）は使わない。"""
        same = [p for p in profiles if p.agent == self.opt.judge_agent]
        if self.opt.judge_profile:
            hit = [p for p in profiles if p.id == self.opt.judge_profile]
            if not hit:
                raise SpecError(f"--judge-profile '{self.opt.judge_profile}' がプロファイルにありません")
            if hit[0].agent != self.opt.judge_agent:
                raise SpecError(f"--judge-profile '{self.opt.judge_profile}' は {hit[0].agent} 用です（--judge-agent {self.opt.judge_agent}）")
            return hit[0]
        base = [p for p in same if p.id == self.opt.baseline]
        return (base or same or [None])[0]

    # ------------------------------------------------------------ plan
    def plan(self) -> dict[str, Any]:
        """実行前の見積もり（何回エージェントを呼ぶか、キャッシュで何回省けるか、概算費用）。"""
        hist = self._cost_history()
        rows, calls, hits, est, unknown = [], 0, 0, 0.0, 0
        for t, p in self.pairs:
            for i in range(self.opt.trials):
                key = cache_key(t, p, self.versions[p.id], i, self.opt, self.judge_sig)
                if self.opt.use_cache and self._cache_get(key):
                    hits += 1
                    continue
                calls += 1
                c = hist.get((p.id, t.id)) or hist.get((p.id, None))
                if c is None:
                    unknown += 1
                else:
                    est += c
            rows.append((t.id, p.id))
        return {"pairs": len(rows), "trials_total": len(rows) * self.opt.trials, "agent_calls": calls,
                "cache_hits": hits, "estimated_cost_usd": est, "unknown_cost_calls": unknown,
                "llm_judge": self.opt.llm and any(uses_llm(t) for t, _ in self.pairs),
                "cli_versions": self.versions}

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

    # ------------------------------------------------------------ judge
    def _judge_runner(self) -> Callable[[str], tuple[str | None, float | None]]:
        src = self.judge_profile
        adapter = get_adapter(self.opt.judge_agent, src, executable=self.opt.judge_executable)
        # 認証まわりだけ借りた判定専用プロファイル（配布物・追加引数・モデル指定は使わない）
        prof = Profile(id="judge", agent=self.opt.judge_agent, path=src.path if src else self.run_dir / "judge.yaml",
                       env=dict(src.env) if src else {}, env_unset=list(src.env_unset) if src else [],
                       copy_credentials=src.copy_credentials if src else False,
                       isolate_home=src.isolate_home if src else True)

        def run(prompt: str) -> tuple[str | None, float | None]:
            with self.lock:
                self._judge_seq += 1
                n = self._judge_seq
            tp = make_trial_paths(self.run_dir / "judge" / str(n), adapter.config_dirname)
            try:
                env = build_env(prof, tp, adapter.config_env)
                if prof.copy_credentials:
                    copy_credentials(prof.agent, tp)
                pr = run_process(adapter.judge_cmd(self.opt.judge_model), tp.workdir, env, prompt, tp.stdout, tp.stderr, 300)
                out = tp.stdout.read_text(encoding="utf-8", errors="replace") if tp.stdout.exists() else ""
                err = tp.stderr.read_text(encoding="utf-8", errors="replace") if tp.stderr.exists() else ""
            finally:
                shutil.rmtree(tp.root, ignore_errors=True)
            if pr.launch_error:
                raise JudgeUnavailable(f"判定 CLI を起動できません: {pr.launch_error}")
            if pr.timed_out:
                raise JudgeUnavailable("判定が 300 秒でタイムアウト")
            text, cost, jerr = adapter.parse_judge(out)
            self._spend(cost)
            if jerr or not text:
                raise JudgeUnavailable(f"判定が失敗: {jerr or '応答なし'} {(classify_stderr(err) or '')} {err.strip()[-200:]}".strip())
            return text, cost
        return run

    # ------------------------------------------------------------ trial
    def _execute(self, task: Task, profile: Profile, trial: int, key: str) -> TrialRecord:
        adapter = get_adapter(profile.agent, profile)
        tdir = self.run_dir / "trials" / task.id / profile.id / str(trial)
        tp = make_trial_paths(tdir, adapter.config_dirname)
        try:
            return self._execute_in(task, profile, trial, key, adapter, tdir, tp)
        finally:
            # 一時ホームは認証情報の複製を含みうるので、例外・中断（Ctrl-C）でも必ず消す
            shutil.rmtree(tp.home, ignore_errors=True)

    def _execute_in(self, task: Task, profile: Profile, trial: int, key: str, adapter, tdir: Path, tp) -> TrialRecord:
        rec = self._record(task, profile, trial, key, trial_dir=str(tdir), started_at=datetime.now(timezone.utc).isoformat())
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
        ph = placeholders(tp, profile)
        cmd = adapter.build_cmd(task, profile, tp.workdir, [fill(a, ph) for a in profile.extra_args])
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
        if etype:
            rec.error = {"type": etype, "message": (emsg or "")[:1000]}
        rec.valid = etype not in EXCLUDED_ERRORS
        if rec.valid:
            judge = JudgeConfig(enabled=self.opt.llm, run=self._judge_runner() if self.opt.llm else None)
            try:
                grade_all(task.graders, rec, tp.workdir, judge, task.path.parent)
            except JudgeUnavailable as e:  # 判定側の環境問題は評価対象の失敗ではないので除外（キャッシュもしない）
                rec.error, rec.valid = {"type": "judge_error", "message": str(e)[:1000]}, False
            if rec.valid and rec.score is None and rec.error is None:  # 採点できるグレーダーが 1 つもない（例: llm だけのタスクを --no-llm）
                rec.error, rec.valid = {"type": "not_graded", "message": "実行されたグレーダーがありません"}, False
            rec.passed = rec.valid and rec.error is None and rec.score == 1.0
            rec.cmp_passed = (rec.valid and rec.error is None and rec.cmp_score == 1.0) if rec.cmp_score is not None else None
            if rec.valid and not rec.passed:
                tags = [etype] if etype else []
                tags += [g["category"] for g in rec.grader_results if g["status"] in ("fail", "unknown")]
                rec.failure_tags = list(dict.fromkeys(tags))
        (tp.root / "record_for_grader.json").unlink(missing_ok=True)
        return rec

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
        key = cache_key(task, profile, self.versions[profile.id], trial, self.opt, self.judge_sig)
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
            with self.lock:
                over = self.opt.max_cost_usd is not None and self.spent >= self.opt.max_cost_usd
            if over:
                rec = self._record(task, profile, trial, key, valid=False,
                                   error={"type": "budget_stop", "message": "費用の上限（--max-cost-usd / --monthly-budget-usd）に到達したため未実行"})
                sink(rec)
                return rec
            rec = self._execute(task, profile, trial, key)
            self._spend(rec.cost_usd)
            if self.interrupted.is_set():  # Ctrl-C で止めた試行は結果ではない（失敗として数えず、キャッシュもしない）
                rec.valid, rec.passed, rec.score, rec.failure_tags = False, False, None, []
                rec.error = {"type": "interrupted", "message": "Ctrl-C で中断"}
            retryable = not rec.valid and (rec.error or {}).get("type") in ("rate_limit", "overloaded")
            if retryable and attempt < self.opt.retry_infra:
                attempt += 1
                self.opt.log(f"  再試行 {attempt}/{self.opt.retry_infra}: {task.id}/{profile.id}#{trial} ({rec.error})")
                self.interrupted.wait(self.opt.retry_wait_s * attempt)  # Ctrl-C ですぐ抜ける
                continue
            break
        self._cleanup(rec)
        self._cache_put(rec)
        sink(rec)
        return rec

    def run(self) -> tuple[Path, list[TrialRecord], dict[str, Any]]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc).isoformat()
        records: list[TrialRecord] = []
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
            shutil.rmtree(self.run_dir / "judge", ignore_errors=True)
        records.sort(key=lambda r: (r.profile_id, r.task_id, r.trial))
        summary = summarize([r for r in records if (r.error or {}).get("type") not in NOT_RUN_ERRORS], self.opt.kk)
        meta = {
            "run_id": self.run_id, "tool_version": __version__, "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "platform": f"{platform.system()} {platform.release()}", "python": sys.version.split()[0],
            "options": {"trials": self.opt.trials, "k": self.opt.kk, "tags": self.opt.tags, "llm": self.opt.llm,
                        "judge_agent": self.opt.judge_agent, "judge_model": self.opt.judge_model,
                        "judge_profile": self.judge_profile.id if self.judge_profile else None,
                        "jobs": self.opt.jobs,
                        "max_cost_usd": self.opt.max_cost_usd, "monthly_budget_usd": self.opt.monthly_budget_usd,
                        "month_spent_before_run_usd": self.month_spent, "use_cache": self.opt.use_cache,
                        "baseline": self.opt.baseline, "seed": self.opt.seed},
            "cli_versions": self.versions,
            "profiles": {p.id: {"agent": p.agent, "hash": p.content_hash(), "model": p.model, "path": str(p.path)}
                         for p in {p.id: p for _, p in self.pairs}.values()},
            "tasks": {t.id: {"hash": t.content_hash(), "tags": t.tags, "paired_negative": t.paired_negative, "llm": uses_llm(t)}
                      for t in {t.id: t for t, _ in self.pairs}.values()},
            "spent_usd_this_run": self.spent,
            "not_run": sum(1 for r in records if (r.error or {}).get("type") in NOT_RUN_ERRORS),
        }
        summary["meta"] = meta
        (self.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.run_dir, records, summary


def load_run(run_dir: Path) -> tuple[list[TrialRecord], dict[str, Any]]:
    recs = [TrialRecord.from_dict(json.loads(l)) for l in
            (run_dir / "trials.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    return recs, summary
