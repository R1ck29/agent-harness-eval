# 評価の既定と採用ゲート

- [x] 124b8e1 の差分と残りの実行経路を確認
- [x] verdict-core を独立レビュー（graph-reviewer: FAIL → 判定不一致を合格に数える欠陥を cc15927 で修正）
- [x] 判定役を runner に接続（決定的な検査が先・最小の入力・盲検・seed 順・多数決・別系統/両系統一致・校正・判定キャッシュ）
- [x] 公開価格表（prices.json）と solver / judge の使用量・費用、実行前の見積もり（段階ごと）、--max-cost-usd で開始前に停止
- [x] run の事前登録（prereg.json）、verdict サブコマンドと終了コード、HTML / JSON / CLI の冒頭に結論
- [x] README と HTML ガイド・リファレンス・バージョン 0.10.0（abaaecf をコードと突き合わせて bc07eac で修正。見つかった不具合 2 件は 069d0a7）
- [x] a〜c を旧と新で記録（a・c は実データ、b は合成）。d はモック + 外側のシェルの手順（実呼び出しは認証なし・利用上限）
- [x] Windows CI の赤: gh run view 44509877119 で 6 件を確認。すべて main のテストと harvest（このブランチは未変更）が Windows で初めて走ったため。PR #3 の ae70cd7 と同じ 6 件 → cherry-pick（d4f4a53）
- [x] ズル対策（CHEATPROOF.md §4）
  - [x] cheatproof.py: leak 検査（vl の leak_check と同じ規則 + カナリア + 実パス: 本物のホーム・--out・タスクのフォルダ）→ valid=false, invalid_reason=leak
  - [x] tamper 検査（試行前の作業ツリーの写しと比べる。git の履歴は使わない）→ 採点の前、pass=false + test_tamper
  - [x] 採点の一時ディレクトリを試行ごとに作って消す（command グレーダーに TMPDIR・TEMP・TMP）
  - [x] 環境変数の許可リスト（PATH・LANG・LC_*・TERM・TZ・SHELL・OS の必須・プロキシと証明書 + inherit_env）
  - [x] sandbox: bwrap（Linux 既定。使えなければ実行前に止める。macOS/Windows は警告 + leak 検査）
  - [x] Codex の web_search を切る（タスクが WebSearch/WebFetch を許すときだけ live）
  - [x] 認証の複製: 期限の確認（近ければ止める）・試行中の更新をロック付き・原子的に書き戻す・doctor に期限
  - [x] verdict: leak を無効に数え、検査欄に leak / tamper / 隔離なしを出す
  - [x] メタ評価（モデルなし）: 参照解を読む模擬エージェント → INVALID、テストを消す模擬エージェント → 不合格。docs/results に記録
- [x] 全テスト（253 passed / 1 skipped）・ruff・CI 6 ジョブ緑（run 44609206157）
- [ ] PR 作成（マージしない）

## レビュー結果

- 124b8e1 に判定エンジンと選択・盲検の部品があった。PR #3 が 0.9.0 を使用中のため 0.10.0 にする
- 既定の段階は dev（Riku の費用ルール）。LLM 判定が合否を決めた結論は dev では暫定、verdict（ゲート）は decisive を要求
- LLM 判定が合否を決めていない実行（決定的な採点器だけ）は段階に関係なく採用できる
- graphctl: verdict-core の FAIL を記録できず（証拠の形式）、試行上限 2/2 のため人の grant-attempt が必要

## 続き（11/20 00:57 JST、Claude 5h 上限で停止。次は 05:20 JST 以降の新しいセッション）
- 停止時点: 77d1206（leak/tamper/env 許可リスト/試行ごと TMPDIR/web_search）・dd511f8（verdict の完全性検査・模擬ズルエージェントのメタ評価・sandbox/認証書き戻し/web_search のテスト）は push 済み。README/guide/reference の未コミット差分はこの WIP コミットに入れた（レビュー未了）。252 passed / 1 skipped。
- 残り: 上のズル対策チェックリストを実装と突き合わせて [x] に更新・不足分の実装（特に bwrap の実体と doctor の期限表示の確認）、docs/results/2026-11-19-sound-eval-defaults.md にメタ評価の結果を記録、ruff・CI（ubuntu/macos/windows）緑の確認、PR 作成（`gh pr create --base main`、本文に評価 a〜d + メタ評価 + 残課題 + EVAL-GATE 行）。マージしない。
- **CI 回帰（最優先で直す）**: 069d0a7 は 6 ジョブ全部緑だったが、77d1206/dd511f8 以降 macOS・Windows が赤（run 44560124361; ubuntu は緑）。
  - macOS: tests/test_cheatproof.py::LeakCheck::test_own_dir_glob_is_allowed が 'INVALID' != 'clean'（推定: /var → /private/var の実パス化か大文字小文字でパス比較がずれる。要確認）。
  - Windows: test_e2e の多数が agent_crash（20/20 など）→ 推定: 環境変数の許可リストが Windows に必要な変数（SYSTEMROOT・COMSPEC・PATHEXT・USERPROFILE・APPDATA・TEMP など）を落とした。要確認。

## 続き（11/20 05:4x JST）
- CI 回帰は修正済み（Windows: 許可リストに PYTHONUTF8 / PYTHONIOENCODING / PYTHONLEGACYWINDOWSSTDIO、macOS: leak 検査のグロブを実パスと渡された表記の両方で作る）。6 ジョブ緑（run 44609206157）。
- ズル対策チェックリストは実装・テスト（tests/test_cheatproof.py 35 件）と突き合わせて [x]。メタ評価は docs/results/2026-11-19-sound-eval-defaults.md の e に記録。
- 残り: PR 作成（`gh pr create --base main`、マージしない）。Codex 実測は Riku の codex login 待ち、graphctl verdict-core は grant-attempt 待ち。

## グラフの状態（11/20 午前）
- 検証済み: verdict-core・cost-usage・judge-pipeline・cli-report・docs-version（それぞれ graph-reviewer の PASS。HEAD 546d967 で pytest 253 passed / 1 skipped・ruff 緑を再実行して確認）。
- evidence は FAIL（試行 1/2）: 基準 b が「実データで IMPROVED」なのに、実データでは条件（ベースライン合格率 20〜80%）を満たす実行がなく合成データで示している。a・c・d は PASS。pr は evidence に依存するため無効化中。
- 解除には Riku の判断が要る: (1) b を満たす実データの実行（外側のシェルで。手順は docs/results の b）を行う、または (2) 基準 b を「合成データ可（出所明記）」に人が直す。直したあと `graphctl retry evidence` → 提出 → レビュー → `retry pr` → 検証。
- レビューで出た軽微な穴（PR の残課題）: 開始前の費用上限の判定は履歴のない solver の呼び出しを $0 と数える（警告は出る）、実行中の JudgeBudgetStop の直接テストなし、a の 8 タスク verdict の生出力が evidence に別名で残っていない。

## 続き（11/20 午前・d の実測を外側のシェルで行えるようにする）
- 判定役専用の `credentials: in_place`（認証を複製せず、本物の CODEX_HOME をその場で使う。codex だけ・判定役だけ）、`--judge-profile` に YAML のパスと `系統=参照`、`examples/judge-profiles/`、費用上限の穴 2 件（履歴のない solver 呼び出しの既定見積もり・JudgeBudgetStop の直接テスト）を追加。グラフは `task-graph-d.json`（前の `task-graph.json` の evidence は人の判断待ちのまま）。
- 本物の `~/.codex`・`~/.claude` は触っていない（テストは偽の CODEX_HOME と偽の CLI だけ）。入れ子の claude/codex のモデル呼び出しなし。

## 外側のシェルで実行するコマンド（d の実測。判定は合計 8 回以内（任意の確認を足して 9 回）・`--max-cost-usd 3`）
前提: `codex login` 済み（ChatGPT のログイン。`~/.codex/auth.json`）、`claude setup-token` で作ったトークンを `CLAUDE_CODE_OAUTH_TOKEN` に入れる（値はファイル・ログに書かない）、`bwrap` がある。ブランチ `feat/sound-eval-defaults` のリポジトリのルートで。

```
OUT=/tmp/ahe-d                      # リポジトリとホームの外
mkdir -p "$OUT"
export CLAUDE_CODE_OAUTH_TOKEN=...  # claude setup-token の出力
P=examples/judge-profiles
SOLVERS="$P/claude-baseline-oauth.yaml $P/claude-ds-lite-oauth.yaml"
TASKS="--tasks examples/tasks --task csv-summary-format --task csv-summary-insight"
COMMON="$TASKS --profiles $SOLVERS --baseline claude-baseline --trials 1 --calibration-items 2 --max-cost-usd 3 --out $OUT --cache-dir $OUT/cache"

# 1. 見積もり（モデルは呼ばない）
python -m harness_eval plan $COMMON --judge-profile codex=$P/codex-judge-inplace.yaml

# 2. 実行 A: 新既定（dev。別系統の Codex が判定。認証は in_place = 複製なし）
python -m harness_eval run $COMMON --tier dev --judge-profile codex=$P/codex-judge-inplace.yaml

# 3. 実行 B: 旧既定に相当（haiku・同系統。解く側の試行は A のキャッシュを使う）
python -m harness_eval run $COMMON --judge-agent claude_code --judge-model haiku --allow-weak-judge \
  --judge-profile claude_code=$P/claude-judge-oauth.yaml

# 4. 一致率と判定の費用
python - <<'PY'
import json, glob, os
for f in sorted(glob.glob(os.path.expandvars("$OUT/2*/summary.json")))[-2:]:
    s = json.load(open(f)); j = s["meta"]["judge"]; u = s["usage"]
    print(f, j.get("models"), s["meta"]["options"]["judge_credentials"], j.get("calibration"), u["judge"], u["solver"]["cost_usd"], u["judge_share"])
PY
```
- 任意（1 回）: `~/.codex/AGENTS.md` が判定に入るかの確認。`HOME=$(mktemp -d) codex exec --json --ephemeral -s read-only --skip-git-repo-check --ignore-user-config --ignore-rules --disable hooks -C "$(mktemp -d)" "渡されている指示文の見出しだけを列挙して。なければ none"`
- 結果は docs/results/2026-11-19-sound-eval-defaults.md の「d」に追記する（一致率・費用・in_place が効いたか・AGENTS.md の有無）。`run` が「認証が…期限」で止まったら `codex login` してやり直す。
