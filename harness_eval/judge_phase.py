"""LLM 判定の実行（Runner の一部）: 決定的な検査のあと、判定待ちの入力だけを盲検化・順序のランダム化・多数決・両系統一致で判定し、
判定キャッシュ（判定入力 + 判定モデル + ルーブリック + 段階のハッシュ）で同じ出力の再判定を省く。校正（決定的な採点器との一致率）と
実行前の判定費用の見積もりもここ。"""
from __future__ import annotations

import contextlib
import json
import os
import random
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import credentials
from .adapters import ClaudeAdapter, CodexAdapter, classify_stderr, get_adapter
from .costs import add_usage, judge_call_estimate, token_cost
from .graders import JUDGE_PROMPT, JudgeUnavailable, parse_verdict
from .isolation import (build_env, bwrap_problem, copy_credentials, fill, make_trial_paths, release_home, route_signature, run_process,
                        sandbox_cmd, sandbox_mode)
from .judging import (FAMILIES, TIER_MODELS, blind, combine_rubric, dedupe_calibration, judge_cache_key, judge_identity, majority,
                      make_settings, pick_calibration)
from .record import TrialRecord
from .spec import ROUTE_VARS, Profile, SpecError


class JudgeBudgetStop(Exception):
    """費用の上限に達したため判定を実行しなかった（試行は budget_stop として除外）。"""


class JudgePhase:
    """Runner に混ぜて使う（self.opt・self.judge・self.lock・self._spend などは Runner が持つ）。"""

    def judge_estimate(self) -> dict[str, Any]:
        """判定側の見積もり（上限）: 決定的な検査がすべて合格したと仮定した判定回数 × 1 回あたりの費用。段階ごと（dev / decisive）。
        判定キャッシュに同じ入力があれば実際の呼び出しはこれより少ない。1 回あたりは判定キャッシュの実測の平均、なければ上限寄りの仮定。"""
        hist = self._judge_history()
        avail = list(self.judge.available) if self.judge else []
        out: dict[str, Any] = {}
        for tier in ("dev", "decisive"):
            try:
                st = self.judge if self.judge and self.judge.tier == tier else make_settings(
                    tier, {p.agent for _, p in self.pairs}, avail, self.opt.judge_agent,
                    None, None, True, self.opt.seed, self.opt.calibration_items)
            except SpecError:
                continue
            n_calls, cost, unknown, basis = 0, 0.0, 0, set()
            for t, p in self.pairs:
                rubric = [g for g in t.graders if g.type == "llm"]
                if not rubric:
                    continue
                for f in st.families_for(p.agent):
                    model = st.models.get(f)
                    for g in rubric:
                        chars = len(JUDGE_PROMPT) + len(str(g.params.get("criteria", ""))) + int(g.params.get("max_chars", 20000))
                        c, b = judge_call_estimate(model, f, chars, hist.get((f, str(model))))
                        basis.add(b)
                        k = self.opt.trials * st.votes
                        n_calls += k
                        if c is None:
                            unknown += k
                        else:
                            cost += c * k
            # 校正: LLM 判定が合否を決めた試行があるときだけ、決定的な採点器を持つタスクから最大 calibration_items 件
            cal_tasks = [t for t, _ in self.pairs if t.calibration]
            if cal_tasks and n_calls:
                fams = sorted({f for _, p in self.pairs for f in st.families_for(p.agent)})
                for f in fams:
                    model = st.models.get(f)
                    chars = len(JUDGE_PROMPT) + int(cal_tasks[0].calibration.get("max_chars", 20000))
                    c, b = judge_call_estimate(model, f, chars, hist.get((f, str(model))))
                    k = st.calibration_items * st.votes
                    n_calls += k
                    unknown += k if c is None else 0
                    cost += (c or 0) * k
            out[tier] = {"calls_max": n_calls, "estimated_cost_usd_max": cost, "unknown_cost_calls": unknown,
                         "models": {f: st.models.get(f) for f in st.used_families()}, "votes": st.votes,
                         "families": {a: st.families_for(a) for a in st.plan}, "basis": sorted(basis)}
        return out

    def _judge_history(self) -> dict[tuple[str, str], dict[str, Any]]:
        """判定キャッシュの実測から、系統・モデルごとの 1 回あたりの平均トークン。"""
        acc: dict[tuple[str, str], list[dict[str, Any]]] = {}
        d = self.opt.cache_dir / "judge"
        if d.is_dir():
            for f in d.glob("*.json"):
                try:
                    r = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if r.get("tokens"):
                    acc.setdefault((r.get("family"), str(r.get("model"))), []).append(r["tokens"])
        return {k: {**{t: sum(int(x.get(t) or 0) for x in v) / len(v) for t in ("input", "cache_read", "cache_write", "output")},
                    "n": len(v)} for k, v in acc.items()}

    def _judge_profile(self, family: str) -> Profile:
        """判定の呼び出しに使う（認証だけ借りた）プロファイル。配布物・追加引数は使わない。"""
        src = self.judge_profiles.get(family)
        return Profile(id="judge", agent=family, path=src.path if src else self.run_dir / "judge.yaml",
                       env=dict(src.env) if src else {}, env_unset=list(src.env_unset) if src else [],
                       # 借りるプロファイルがなければ、その系統の経路変数（ROUTE_VARS）だけを引き継ぐ（親環境の他の秘密は渡さない）
                       inherit_env=list(src.inherit_env) if src else list(ROUTE_VARS[family]),
                       copy_credentials=src.copy_credentials if src else False, credentials=src.credentials if src else None,
                       isolate_home=src.isolate_home if src else True,
                       sandbox=src.sandbox if src else "bwrap",
                       sandbox_ro=list(src.sandbox_ro) if src else [])

    def _resolve_judge_adapter(self, family: str) -> ClaudeAdapter | CodexAdapter | None:
        src = self.judge_profiles.get(family)
        requested = self._judge_exe(family) or (src.executable if src else None)
        adapter = get_adapter(family, executable=requested)
        prof = self._judge_profile(family)
        path = os.defpath if "PATH" in prof.env_unset else os.environ.get("PATH", os.defpath)
        if "PATH" in prof.env:
            path = fill(prof.env["PATH"], {"profile_dir": str(prof.base_dir), "python": sys.executable})
        found = shutil.which(adapter.exe[0] if requested else adapter.command, path=path)
        if found is None:
            return None
        entry = Path(found)
        # Preserve the selected shim name while resolving relative or symlinked parent directories.
        adapter.exe[0] = str(entry.parent.resolve() / entry.name)
        return adapter

    def _identity(self, family: str, model: str | None) -> str:
        """判定役の識別子（実行ファイル・CLI の版・実効モデル・非秘密の経路設定）。判定キャッシュのキーに入れる。"""
        k = (family, model)
        if k not in self._identities:
            adapter = self._judge_adapters.get(family)
            if adapter is None:
                raise JudgeUnavailable(f"判定役の CLI が見つかりません: {family}")
            prof = self._judge_profile(family)
            self._identities[k] = judge_identity(list(adapter.exe), self.judge_versions.get(family), model,
                                                 route_signature(prof, dict(os.environ))
                                                 + ("|in_place" if prof.credentials == "in_place" else ""))
        return self._identities[k]

    def _judge_over_budget(self, family: str) -> bool:
        """費用の上限（--max-cost-usd・月の予算）に達しているか。月の予算は billing: api の口座だけ。"""
        billing = self._judge_billing(family) if hasattr(self, "_judge_billing") else "api"
        with self.lock:
            if self.opt.max_cost_usd is not None and self.spent >= self.opt.max_cost_usd:
                return True
            if billing != "api":
                return False
            rem = self.month_remaining(family)
            return rem is not None and self.spent_api[family] >= rem

    def _settle_judge(self, adapter: Any, family: str, model: str | None, out: str) -> tuple[str | None, dict[str, Any], float | None, str | None]:
        """判定 1 回の応答を読み、トークン・費用を使量・予算・台帳に計上する。(応答, トークン, 費用, 失敗理由)。"""
        text, cli_cost, jerr = adapter.parse_judge(out)
        tokens = adapter.judge_tokens(out)
        cost = token_cost(tokens, model, family)
        if cost is None:
            cost = cli_cost
        with self.lock:
            add_usage(self.judge_usage, tokens, cost)
            bm = self.judge_usage["by_model"].setdefault(f"{family}:{model}", {"calls": 0, "cost_usd": 0.0})
            bm["calls"] += 1
            bm["cost_usd"] += cost or 0.0
        self._spend(cost, family, self._judge_billing(family))
        return text, tokens, cost, jerr

    def _judge_call(self, family: str, model: str | None, prompt: str) -> tuple[str, dict[str, Any], float | None]:
        """判定 CLI を 1 回呼ぶ（認証だけ借りた判定専用の一時ホーム。配布物・追加引数は使わない）。(応答, トークン, 公開価格での費用)。"""
        prof = self._judge_profile(family)
        adapter = self._judge_adapters[family]
        in_place = prof.credentials == "in_place"
        with self.lock:
            self._judge_seq += 1
            n = self._judge_seq
        tp = make_trial_paths(self.run_dir / "judge" / str(n), adapter.config_dirname)
        try:
            env = build_env(prof, tp, adapter.config_env)
            if in_place:
                # 認証は複製しない: HOME は一時ホームのまま、設定ディレクトリだけ本物を指す（本物への書き込みは CLI 自身の更新だけ）。
                # 更新（refresh）の競合を避けるため、同じ系統では同時に 1 本だけ呼ぶ
                real = credentials.source_path(family)
                if real is None:
                    raise JudgeUnavailable(f"{family} は credentials: in_place に対応していません")
                env[adapter.config_env] = str(real.parent)
            elif prof.copy_credentials:
                copy_credentials(prof.agent, tp)
            cmd = adapter.judge_cmd(model, in_place=True) if in_place else adapter.judge_cmd(model)
            if not in_place and sandbox_mode(prof) == "bwrap" and bwrap_problem() is None:
                cmd = sandbox_cmd(cmd, tp, env, prof, n_exe=len(adapter.exe))
            settled = None
            with self._inplace_locks[family] if in_place else contextlib.nullcontext():
                if in_place and self._judge_over_budget(family):
                    # 直列に待っていた間に、先の判定が残額を使い切ったかもしれない: 起動の直前に、ロックの中で確認する
                    raise JudgeBudgetStop("費用の上限に到達したため判定を実行していません")
                pr = run_process(cmd, tp.workdir, env, prompt, tp.stdout, tp.stderr, 300)
                out = tp.stdout.read_text(encoding="utf-8", errors="replace") if tp.stdout.exists() else ""
                err = tp.stderr.read_text(encoding="utf-8", errors="replace") if tp.stderr.exists() else ""
                if not pr.launch_error and (in_place or pr.timed_out):
                    settled = self._settle_judge(adapter, family, model, out)  # 費用を計上してから次の呼び出しを許可する
        finally:
            release_home(tp, self.opt.log)
            shutil.rmtree(tp.root, ignore_errors=True)
        if pr.launch_error:
            raise JudgeUnavailable(f"判定 CLI を起動できません: {pr.launch_error}")
        if pr.timed_out:
            raise JudgeUnavailable("判定が 300 秒でタイムアウト")
        text, tokens, cost, jerr = settled or self._settle_judge(adapter, family, model, out)
        if jerr or not text:
            if in_place:  # 実認証を指す CLI の出力（エラーメッセージ・標準エラー）は記録・報告に載せない。分類だけ残す
                raise JudgeUnavailable(f"判定が失敗（in_place: CLI の出力は記録しません）{(classify_stderr(err) or '')}".strip())
            raise JudgeUnavailable(f"判定が失敗: {jerr or '応答なし'} {(classify_stderr(err) or '')} {err.strip()[-200:]}".strip())
        return text, tokens, cost

    def _judge_vote(self, family: str, model: str | None, prompt: str, rubric: str, vote: int) -> dict[str, Any]:
        """1 票。判定キャッシュ（判定入力 + 判定モデル + ルーブリック + 段階 + 何票目かのハッシュ）にあれば呼ばない。"""
        assert self.judge is not None
        key = judge_cache_key(prompt, family, model, rubric, self.judge.tier, vote, self._identity(family, model))
        f = self.opt.cache_dir / "judge" / f"{key}.json"
        if self.opt.use_cache and f.is_file():
            try:
                hit = json.loads(f.read_text(encoding="utf-8"))
                with self.lock:
                    self.judge_usage["cache_hits"] += 1
                return {**hit, "cached": True}
            except (OSError, json.JSONDecodeError):
                pass
        if self._judge_over_budget(family):
            raise JudgeBudgetStop("費用の上限に到達したため判定を実行していません")
        text, tokens, cost = self._judge_call(family, model, prompt)
        status, reason = parse_verdict(text)
        if self._judge_profile(family).credentials == "in_place":
            reason = "（in_place の判定: 理由は記録しません）"  # 実認証を指す CLI の出力は、記録・キャッシュ・報告に載せない
        row = {"family": family, "model": model, "tier": self.judge.tier, "vote": vote, "status": status, "reason": reason,
               "tokens": tokens, "cost_usd": cost, "at": datetime.now(timezone.utc).isoformat()}
        if self.opt.use_cache:  # 判定の応答の本文・判定入力は残さない（キーはハッシュ）
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        return {**row, "cached": False}

    def _blind_terms(self) -> tuple[list[str], list[str]]:
        profiles = list({p.id: p for _, p in self.pairs}.values())
        terms = [p.id for p in profiles] + [p.model for p in profiles if p.model] + list(FAMILIES)
        terms += [m for tier in TIER_MODELS.values() for m in tier.values()]
        for p in profiles:  # 配布物の名前（スキル・ファイル名など）もどの条件かを教えうる
            for it in p.install:
                if not isinstance(it.get("copy"), str):
                    continue
                src = Path(it["copy"].replace("{profile_dir}", str(p.base_dir)))
                src = src if src.is_absolute() else p.base_dir / src
                names = [src.name, Path(str(it.get("to", ""))).name]
                if src.is_dir():  # skills/ を丸ごと複製する場合は、中のスキル名・ファイル名も
                    names += [c.name for c in src.iterdir()]
                terms += [n for n in names if n and "{" not in n]
        paths = [str(self.run_dir), str(self.opt.cache_dir), str(Path.home())] + [str(p.base_dir) for p in profiles]
        return terms, paths

    def _judge_prompt(self, rec: TrialRecord, item: dict[str, Any]) -> str:
        """判定役に渡すプロンプト（盲検化済み）。校正の重複除去にも使う。"""
        terms, paths = self._blind_terms()
        paths = paths + [p for p in (rec.trial_dir,) if p]
        return JUDGE_PROMPT.format(criteria=item["criteria"], focus=blind(item["focus"], terms, paths),
                                   content=blind(item["content"], terms, paths))

    def judge_one(self, rec: TrialRecord, item: dict[str, Any]) -> str:
        """1 つの判定入力を、評価対象と別系統の判定役で（decisive で両系統あれば両系統で）各 votes 回判定し、まとめた結果を返す。"""
        assert self.judge is not None
        fams = self.judge.families_for(rec.agent)
        if not fams:
            raise JudgeUnavailable("判定役の CLI が見つかりません")
        prompt = self._judge_prompt(rec, item)
        per: dict[str, Any] = {}
        for f in fams:
            model = self.judge.models.get(f)
            votes = [self._judge_vote(f, model, prompt, item["criteria"], v) for v in range(self.judge.votes)]
            with self.lock:  # この試行の判定費用（キャッシュから再利用した票は 0。費用が分からない票があれば None）
                for v in votes:
                    if not v["cached"]:
                        c = v.get("cost_usd")
                        rec.judge_cost_usd = None if c is None or rec.judge_cost_usd is None else rec.judge_cost_usd + c
            per[f] = {"model": model, "votes": [v["status"] for v in votes], "majority": majority([v["status"] for v in votes]),
                      "cached": sum(v["cached"] for v in votes), "reason": votes[0].get("reason", "")}
        status = combine_rubric({f: v["majority"] for f, v in per.items()}, bool(item.get("negate")))
        rec.judge_votes.append({"kind": item["kind"], "grader": item.get("grader"), "status": status,
                                "families": {f: {k: v[k] for k in ("model", "votes", "majority", "cached")} for f, v in per.items()}})
        item["_reason"] = "; ".join(f"{f}: {v['majority']}（{'/'.join(v['votes'])}）{v['reason']}" for f, v in per.items())[:300]
        return status

    def _judge_phase(self, records: list[TrialRecord]) -> None:
        """決定的な検査のあと、判定待ちの入力だけを判定する（判定順は seed で並べ替える）。続けて校正（決定的な採点器との一致率）。"""
        work = [(r, it) for r in records if r.valid for it in r.judge_inputs if it["kind"] == "rubric"
                and r.grader_results[it["grader"]]["status"] == "pending"]
        if not work:
            return
        if self.judge is None or not self.opt.llm:  # --no-llm（キャッシュの試行に判定待ちが残っていた）
            for r, it in work:
                r.grader_results[it["grader"]].update(status="not_run", reason="--no-llm")
            return
        random.Random(f"judge-order:{self.judge.seed}").shuffle(work)
        failed: dict[int, tuple[str, str]] = {}

        def one(r: TrialRecord, it: dict[str, Any]) -> None:
            if id(r) in failed or self.interrupted.is_set():
                return
            try:
                st = self.judge_one(r, it)
                r.grader_results[it["grader"]].update(status=st, reason=it.pop("_reason", ""))
            except JudgeBudgetStop as e:
                failed[id(r)] = ("budget_stop", str(e))
            except JudgeUnavailable as e:
                failed[id(r)] = ("judge_error", str(e)[:1000])

        with ThreadPoolExecutor(max_workers=max(1, self.opt.jobs)) as ex:
            for fut in [ex.submit(one, r, it) for r, it in work]:
                fut.result()
        for r in {id(r): r for r, _ in work}.values():
            if id(r) in failed:
                t, m = failed[id(r)]
                r.error, r.valid, r.passed, r.cmp_passed, r.score, r.cmp_score = {"type": t, "message": m}, False, False, None, None, None
                continue
            self._finalize(r, (r.error or {}).get("type"))
        self._calibrate(records)

    def _calibrate(self, records: list[TrialRecord]) -> None:
        """校正: LLM 判定が合否を決めた試行があるときだけ、決定的な採点器を持つタスクの出力（合格・不合格の両方）を判定役にも判定させ、
        決定的な採点器の合否との一致率を測る（UNKNOWN・判定不一致は不一致として数える）。試行の合否には使わない。"""
        assert self.judge is not None
        decided = any(g.get("type") == "llm" and g.get("status") in ("pass", "fail", "unknown", "judge_disagreement")
                      for r in records if r.valid for g in r.grader_results)
        items = [(r, it) for r in records if r.valid for it in r.judge_inputs if it["kind"] == "calibration"]
        if not decided:
            self.calibration = {"needed": False, "items": 0, "agree": 0, "rate": None,
                                "note": "LLM 判定が合否を決めた試行がないため校正は不要"}
            return
        # 盲検化後の判定入力と評価観点が同じ項目は 1 件に数える（同じ出力の繰り返しを独立した実績にしない）
        answer = lambda x: bool(x[1]["answer"])
        pool = dedupe_calibration(items, lambda x: self._judge_prompt(*x), answer)
        if pool is None:
            self.calibration = {"needed": True, "items": 0, "agree": 0, "rate": None, "available": len(items), "conflicting_answers": True}
            return
        chosen = pick_calibration(pool, self.judge.calibration_items, self.judge.seed, answer)
        agree = done = 0
        by: dict[str, int] = {}
        n_class = {"pass": 0, "fail": 0}
        agree_class = {"pass": 0, "fail": 0}
        for r, it in chosen:
            try:
                st = self.judge_one(r, it)
            except (JudgeUnavailable, JudgeBudgetStop):
                continue
            want = "pass" if it["answer"] else "fail"
            done += 1
            agree += st == want
            n_class[want] += 1
            agree_class[want] += st == want
            k = f"決定的={want} / 判定役={st}"
            by[k] = by.get(k, 0) + 1
        self.calibration = {"needed": True, "items": done, "agree": agree, "rate": agree / done if done else None,
                            "available": len(items), "unique": len(pool), "confusion": by,
                            "pass_items": n_class["pass"], "fail_items": n_class["fail"],
                            "agree_pass": agree_class["pass"], "agree_fail": agree_class["fail"]}
