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
- 結果（11/20）: 6 ノード（spec-inplace・judge-inplace・judge-profile-cli・budget-gaps・docs-examples・ci-pr）を graph-reviewer が独立に確認。276 passed / 1 skipped、ruff 緑、CI 6 ジョブ緑。例のプロファイルは `examples/profiles/` ではなく `examples/judge-profiles/`（既存テストの glob と `--profiles` のディレクトリ展開に巻き込まれ、in_place が解く側として拒否されるため）。既存の `test_e2e` の `--max-cost-usd 0` は、開始前の門と実行中の budget_stop の 2 経路に分けた。`--disable hooks` を判定の呼び出しに足した（hooks を判定に入れない）。
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
python -m harness_eval plan $COMMON --judge-profile codex=$P/codex-judge-inplace.yaml --trust-judge-input

# 2. 実行 A: 新既定（dev。別系統の Codex が判定。認証は in_place = 複製なし）
python -m harness_eval run $COMMON --tier dev --judge-profile codex=$P/codex-judge-inplace.yaml --trust-judge-input

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

## Codex レビュー（XREVIEW-AHE5-astra、CHANGES_REQUESTED・ブロッキング 14 件）への対応（11/20）
グラフは `task-graph-fix.json`（cred-writeback → sandbox-trust → judge-trust → verdict-integrity → cost-integrity → pr-docs）。各ノードを graph-reviewer が独立に確認（sandbox-trust は 1 回目 FAIL: 端末経由の TIOCSTI 脱出と improve.py のリンク追従 → 8d828cd で直して PASS）。

| 指摘 | 対応 | コミット | テスト |
|---|---|---|---|
| 1 CRITICAL 採点の隔離 | command グレーダーを bwrap（実ホーム・他試行・未参照の評価資材・認証なし、ネットワークなし、作業フォルダ ro、環境変数は許可リスト、`--new-session`）で実行。bwrap のない OS は `grader_isolation: none` を記録し、decisive の verdict は INCONCLUSIVE | f460257, 8d828cd, 37fac5c | `test_review5.py::GraderIsolation`（箱の中身・none の記録と verdict・pty で TIOCSTI が効かない） |
| 2 CRITICAL solver の書ける範囲 | bwrap に渡すのは一時ホームと作業フォルダだけ（試行ルートのログ・採点記録は見えず書けない）。ホストの書き込みはリンクをたどらない（`open_new`・`replace_text`・`fresh_dir`・`snapshot` はリンク・FIFO を読まない） | f460257, 8d828cd | `SolverWriteScope`・`HostWritesDoNotFollowLinks` |
| 3 CRITICAL 認証の書き戻し | `give_back` を廃止。複製は捨てるだけ。更新が要る期限なら事前確認で止める | 16f42ac | `test_cheatproof.py::Credentials`（壊れた JSON・空・別トークンでも本物は不変） |
| 4 AWS ファイル | プロファイルが Bedrock を明示したときだけ。`env_unset` は戻さない | f460257 | `AwsFiles` |
| 5 in_place の判定 | `--trust-judge-input` が必須（なければ起動前に SpecError）。in_place の判定の理由は記録しない。README に限界。`env` での `CODEX_HOME` 上書きによる迂回・失敗時の CLI 出力も止める | 8f75b92, d4c94bf | `test_judge_inplace.py::test_in_place_needs_the_judge_input_to_be_declared_trusted` |
| 6 既存テスト本体 | 既存のテストの行の書き換えを `test_body_modified` として検出（確認の数が同じでも） | 96da63e | `test_review5b.py::TestBodyTamper`、`test_cheatproof.py` |
| 7 --no-llm キャッシュ | キャッシュキーに LLM の有無。必須の LLM 判定が未実施の結果は INCONCLUSIVE | 96da63e | `TestNoLlmCacheAndVerdict` |
| 8 cmp_passed=null | 明示的な null は比較から外す。フィールドのない旧記録だけ passed | 96da63e | `TestCmpPassedNull` |
| 9 全滅したタスク | 両条件とも有効な試行がない対象タスクは INCONCLUSIVE | 96da63e | `TestAllInvalidTask` |
| 10 negate | 系統ごとの確定ラベルを反転してから合意（UNKNOWN は票にしない） | 8f75b92 | `NegateAcrossFamilies` |
| 11 判定キャッシュのキー | 実行ファイル（実体・大きさ・更新時刻）・CLI の版・実効モデル・経路を識別子としてキーに | 8f75b92 | `JudgeIdentity`（CLI を取り替えるとキャッシュを使わない） |
| 12 校正 | 盲検化後の入力で重複除去。verdict は合格例・不合格例の両方と各一致率を要求 | 8f75b92 | `CalibrationIndependence` |
| 13 費用 | 呼び出しごとに公開価格で一度確定し、予算・台帳・集計に同じ額（pricing なしの Codex も） | 4808664 | `TestSolverCostSettledOnce` |
| 14 in_place の直列判定 | ロックの中で起動直前に残額を確認し、費用を計上してから次を許可 | 4808664 | `TestInPlaceBudgetInsideLock` |
| 提案: 再試行分の使量 | 捨てた試行の使量を usage.solver に足す | 4808664 | `TestRetryUsageCounted` |

- 見送り（理由つき）: 提案「別 run との認証競合を検出する安定したロック」は、`auth.json` 本体を置き換える CLI の更新と別にロックファイルを作る必要があり、本物の `~/.codex` に新しいファイルを置かない方針と衝突するため見送り（README の運用条件に「単独実行」を明記済み）。
- 隔離された採点が見せるもの: 採点コマンドに書いた実在する絶対パスと `command` グレーダーの `ro:` だけ。`{task_dir}` や `/tmp` のような広いパスを書くとその範囲が見える（タスク作者の責任）。
- 独立レビューの残り（PR の残課題）: 費用が分からない呼び出し（価格表にないモデル）は上限の計算に入らず、上限到達後の起動を止められない（以前からの制約）。tamper はテストファイル名のパターンと「末尾への追記以外の変更」で検出するため、パターンに合わない名前のテストは監視しない。`--no-llm` の結果は `gate`/`promote_decision` の単独実行では弾いていない（verdict だけが INCONCLUSIVE にする）。
- 一時ディレクトリ（レビュアーが作ったもの。削除は未実施）: `/workspace/verify-layer-work/{rev-sbx-x1,exp-judge-trust,vi-export,vi-parent,vi-scripts,vi-scratch,rv-new,rv-old,rv-scripts}`・`/tmp/rev-sbx-*`・`/tmp/jt_*`。

## PR #5 astra r2 再レビュー対応（44718fd）

- [ ] 採点の隔離を検証する回帰テストを先に追加する。旧記録と不明な隔離方式は decisive で保留し、コマンドごとの一時領域と作業ツリー内のパス解決を確認する（ブロッキング 1・2、#1）。
- [ ] 盲検後の正解矛盾を校正不成立とする。判定キャッシュの実行ファイル識別とタイムアウト時の費用を検証する（ブロッキング 3、#11・#12・#14）。
- [ ] solver の出力、Bedrock の環境、末尾のテスト再定義、比較対象外の試行を検証する（#2・#4・#6・#8）。
- [ ] 偽のホームと CLI で ruff・pytest 全件を実行し、独立レビューと未対応事項を記録する。

## PR #5 astra r3 再レビュー対応（36951cf）

- [x] 大量出力の回帰テストを先に追加する。標準出力と標準エラーを両方とも固定長で保存し、タイムアウト時の部分ログを検証する。
- [x] 採点資材のリンク経由参照、既存 Python テスト名の再代入、相対 PATH での判定 CLI の回帰テストを先に追加して修正する。
- [x] 偽ホーム・偽 CLI で ruff と pytest 全件を実行し、独立レビューの結果と残事項を記録する。

### レビュー結果

- `438e953` で親が両パイプから 64 KiB ずつ読み、保護されたログに逐次保存する。片方でも 16 MiB を超えると通常のログ解析と採点を止め、回収できた使用量イベントから試行・台帳・集計の費用を計上する。両パイプ各 8 MiB の実行中保存、17 MiB 超の使用量計上、タイムアウト時の部分ログ、Windows の標準入力切断を模擬したテストを通過した。
- `052c056` で採点資材の探索を workdir の境界または最初のシンボリックリンクで打ち切り、既存 Python テスト名への末尾の再代入を検出する。判定 CLI は選択した名前を保ったまま親ディレクトリを解決し、版確認・キャッシュ識別・起動に同じ絶対パスを使う。
- 両ノードとも独立レビューで PASS。ログの再レビューでは Windows の `EINVAL`、判定 CLI の再レビューではリンク直後の `..` を修正した。関連テストは 35 件成功。
- `uv run --offline --no-sync ruff check .` は成功。pytest 全 331 件は 314 成功、10 失敗、7 スキップ。この環境にある `/tmp` の設定ディレクトリをテストプロセス内だけで無視し、ネットワーク名前空間を使えないため bwrap 不可の経路で確認した。残る 10 件はループバックソケットの権限制限で失敗し、`36951cf` でも同じ 10 件を再現した。Windows 実機は未検証。
- 必須の A・B は対応済み。非ブロッキングの #5 は、`in_place` で認証を参照する CLI 側の読み取りをハーネスから制限できないため、明示的に入力を信頼する運用条件が残る。

## PR #5 opus r4 再レビュー対応（62b715f）

レビュー: `XREVIEW-AHE5-opus-r4.md`（CHANGES_REQUESTED）。対応表（テストは `tests/test_review_r5.py`、ログ超過は `tests/test_review5b.py` も更新）。

| 指摘 | 対応 | コミット | テスト |
|---|---|---|---|
| #2/#6 テスト名の再束縛・削除（ブロッキング） | 静的: for/with/except/match・import の alias・`del`・global/nonlocal 越しの関数本体の代入を同じスコープの束縛として数える。動的: 採点と同じ隔離の子プロセスで pytest 収集し、基準にあった各テストの `co_filename`/`co_firstlineno` を基準の定義と照合（`setattr`・`globals()`・新規 conftest からの代入・収集から消えたテスト）。ホストは solver のコードを import しない。不一致は `test_body_modified` | b4fef1f | `StaticRebindTests`・`DynamicRebindTests` |
| #6 ログ 16 MiB 超の除外（ブロッキング） | 除外（`harness_error`）をやめ、有効な試行の不合格（`log_overflow`、`passed`/`cmp_passed` = False）にする。`invalid_reason` は None、`outcome` は False。使用量・台帳・集計の計上は維持 | 3444614 | `test_oversized_solver_log_keeps_the_usage_and_ledger` |
| #7 drain のディスク書き込み上限 | `run_process(log_limit=16 MiB)`: 上限で書き込みを止めて `ProcResult.log_truncated`。以降もパイプは読み、使用量イベント行だけを 1 MiB まで末尾に残す（1 行 64 KiB 超は捨てる） | 3444614 | `DrainCapTests` |
| #8 waitid WNOWAIT | `_wait_exit`: 終了を回収せずに待つ。`_kill_tree`: 回収前に `killpg`。回収済みのリーダーには送らない。waitid がない OS・Windows は従来経路 | 49108f9 | `KillTreeOrderTests` |
| #9 リンクで捨てた採点資材 | `_grader_material` がリンクで打ち切るとき logging の警告を出す（bind しない方針は維持） | 49108f9 | `GraderMaterialLinkWarningTests` |

- 独立レビュー（graph-reviewer）の指摘による追加修正: 8f49c97（drain の上限がちょうど行末に当たると次の使用量行を落とす）、9d37b71（`test_x.__code__ = …` のようなダンダー属性への書き込みを再束縛として数える。`__init__` を持つクラスは pytest が集めないので基準から外す）。いずれもテストを先に追加。
- キャッシュ: `CACHE_SCHEMA` を 8 → 9（ログ超過の結果の意味が変わり、tamper の検出も広がったため。古い結果は再利用しない）。
- 動的な再束縛の確認は pytest の収集が成功したときだけ行う（pytest がない・収集エラー・タイムアウトは確認できないので何も返さず警告ログ。採点そのものが同じ理由で落ちる）。pytest 以外で走るテスト（JS など）は静的な検出のまま。
- 残る限界: 関数内で `Cls.test_x = f` のように別スコープのクラス属性へ代入しても静的には数えない（動的な照合が捕まえる）。収集で見るのは基準にあったテストだけで、収集の子プロセスを solver が見分けて挙動を変える場合は検出できない。
- レビュアーが挙げた残り（未対応・理由）: conftest の `pytest_pyfunc_call` で本体を飛ばす・`item.runtest` の差し替えは位置が変わらないため検出できない（r4 の指摘範囲外）。`functools.wraps` を使わないデコレータが付いた基準のテストは「収集時に再束縛」と誤検出しうる（まれ。該当タスクは `allow_test_changes` で外せる）。収集の失敗・タイムアウトは確認できないので何も返す（警告ログのみ）。

## PR 5 r6: 実データの偽陽性（tamper の純粋な追加の再判定）

held-out の P0 スモーク（Claude sonnet 素のベースライン、6 罠 × 1）で、隠しテストは 6/6 合格したのに `test_body_modified` が 5/6 を改ざん扱いにした（passed 1/6）。5 件とも solver が正当にテストを足しただけ（既存クラスの途中への新しい `def test_*`、`from operator import mul, eq` への名前の追加＋末尾の新クラス）。行単位の規則が原因。

| 項目 | 対応 | テスト |
|---|---|---|
| 行単位の誤検出 | `changed` / `inserted before the end` に当たった `.py` は AST で再判定（`_pure_addition`）。基準の文が同じ順序・内容（`ast.dump`）で残り、モジュール直下と既存クラスの本体ごとに（ネストも再帰）、許す追加だけが挿入されていれば改ざんにしない。関数の本体は 1 文でも変われば改ざん。許す追加: 既存のモジュール・クラス直下の新しい `test_*`（既存・同じファイルの基底クラスの名前と重ならない・fixture でない）、モジュール直下の新しいクラス、新しい import、既存の `from m import a` への名前の追加。どちらかが構文エラーなら従来どおり改ざん。取り消せるのはこの 2 つの発見だけで、redefined・skip_added・test_checks_net_removed・config narrowing・収集時の位置照合・`allow_test_changes` は変更なし | `tests/test_review_r6.py` |
| 収集時の位置照合（実装時に発見） | `rebound_tests` は基準の行番号と比べていたので、クラス途中への正当な挿入で後ろの行がずれ、静的判定を直しても `rebound at collection` で改ざん扱いのままだった。基準の行ではなく、いまのファイルでの同じ定義（`ast.dump` が同一）の位置と照合する。再束縛すれば位置が変わるので検出力は同じ | `test_a_shifted_definition_…` / `test_a_rebinding_after_a_shift_…` |
| 独立レビューの指摘（補強） | 追加の import の束縛名が、基準が参照している名前（組み込み・`*` import が供給する名前・fixture を受ける引数名）と重なれば改ざん。`fixture` の別名での import は改ざん | `test_added_imports_that_shadow_names_the_tests_only_use` |

- 仕様への上乗せ: 追加 import の名前の衝突を、基準で束縛された名前に加えて基準が参照している名前（組み込みなど）にも広げた。
- 残る穴・限界: ① 追加した test 関数のデコレータ・既定引数・新しいクラスの本体は中身を見ない（モジュールのグローバルや辞書の書き換えは静的にも収集時の位置照合にも出ない。加えて収集時検証（`--collect-only`）の構造的限界として、既存テストより前に挿入した `def test_0():` が収集時には実行されず実採点時だけ先に動いて既存テストの `__code__` を差し替える経路、および `@getattr(pytest, "fix"+"ture")(autouse=True)` のように動的に取得したデコレータが `_is_fixture()` を抜ける件が残る）。② 基底クラスが別ファイルにあるとき、その継承テストの上書きは見えない。③ 末尾への追記は従来の `startswith` 規則で再判定の前に通る（r6 以前から。末尾の import による名前の上書きは static の redefined のみ）。④ 仕様どおり、モジュール直下の新しい補助関数（`def helper()`）・既存クラスへの非テストの追加・`testFoo`（`test_` で始まらない名前）は改ざん扱いのまま。⑤ 既存の `import os` の拡張（`import os, sys`）は許さない（`from m import` だけ）。⑥ 再判定の補強分（上の 3 点）は再レビューしていない。

## PR 5 r7: Gemini r5 の指摘（XREVIEW-AHE5-gemini-r5.md、対象 41555d3）

| 指摘 | 対応 | コミット | テスト |
|---|---|---|---|
| #1 a) `inspect.unwrap` で動的チェックを回避（ブロッキング） | 収集の子プロセスが `__wrapped__` の各層の位置を記録。作業ツリー内（標準ライブラリ・site-packages・.venv の外）で、基準にあったデコレータの内側の関数（関数の中で定義され、いまも同じ内容で残る関数）でもない層があれば `wrapper swapped at collection`。pytest の mark・parametrize・mock.patch は作業ツリーの外 | f5bb9a5 | `tests/test_review_r7.py` DynamicWrapperAndCode |
| #1 b) `__code__` の差し替え | 静的: 既存の名前への dunder 属性の書き込み・削除・`setattr` を、関数・クラスの本体の中でも数える。動的: 収集した関数の `co_code`・定数・名前（再帰）を、基準のソースをコンパイルしたもの（pytest の assert 書き換えあり・なしの両方）と比べる（`code replaced at collection`）。基準をコンパイルできなければ比べない | f5bb9a5 | 同 StaticDunderWritesAnywhere / DynamicWrapperAndCode |
| #1 c) `_python_bindings` の走査漏れ | デコレータ・既定値・注釈・基底・キーワード・lambda の既定・値なし AnnAssign の注釈の中の walrus を束縛として数える。基準になく足された `from m import *` は改ざん | f5bb9a5 | 同 StaticBindingGaps |
| #1 d) fail-open | `rebound_report` が (検出, 確認できなかった理由) を返す（`rebound_tests` は従来どおり検出だけ）。runner は `meta.dynamic_check="unavailable"`・`meta.dynamic_check_reason` と `warnings` に残す。合否は静的検査の結果のまま | f5bb9a5 | 同 DynamicCheckUnavailable・`test_cheatproof.py` |
| #6 デコレータ付きが常に不一致 | **再現せず**: Python 3.8+ の `co_firstlineno` は最初のデコレータの行で、`_baseline_test_positions` の `min(デコレータの行)` は正しい。parametrize・mock.patch・複数・クラスメソッド・ローカルの `functools.wraps` のデコレータ付きが改ざんなしで通る回帰テストだけ追加（#6 の主張に対するコードは変更なし）。ただし #1 a) のラッパー層の判定が、作業ツリー内の既存デコレータを誤検出する退行を独立レビューが見つけたので、AST 同一ではなく基準にあった入れ子の関数・lambda の qualname 一致で受け入れる形に直した。dunder 書き込みの所有者もテスト名・テストを持つクラスに限った | f5bb9a5・bc29b22 | `test_legitimate_decorators_are_not_flagged` |
| #6 副次: `--rootdir` | bwrap では親が見えないので再現しないが、隔離なしでは親の `pytest.ini` で nodeid がずれ全件 `missing` になるのを再現。`--rootdir .` を明示 | f5bb9a5 | `CollectionEnvironment`（隔離なしを強制） |
| #6 副次: `__test__ = False` | 再現（クラス・モジュールとも `missing at collection`）。基準の期待から外す | f5bb9a5 | 同 |
| #7 drain の打ち切り後の取りこぼし | 超過後は 1 行ずつ（上限をまたぐ行も続きを読み切る。1 行の上限は 16 MiB）JSON として読み、トップレベルの type が `result`（最新 1 件・本文を落とし、パーサが使う項目だけ）か `turn.completed`（使用量を足した 1 件）のものだけを末尾に残す。入れ子の文字列で枠を使わない。パイプを握る子孫が残ってタイムアウトした場合も親が書き出す。独立レビューの指摘で、上限が行末（改行の直前）に当たると二重に数える問題を直した（切れた行を NUL で閉じる） | 8eebb21・a726fb0 | `tests/test_review_r7_drain.py`（超過なしの実行と cost_usd・tokens・num_turns が一致） |

- 残る穴・限界: ① `pytest_pyfunc_call` での本体の飛ばし・`item.runtest` の差し替えは、位置も `co_code` も変わらないので検出できない（r6 から継続）。② `__defaults__` / `__kwdefaults__` の動的な差し替えは中身を比べない（静的な書き込みの検出のみ）。③ 基準のコンパイルは pytest の既定の assert 書き換えの有無だけを想定する（`enable_assertion_pass_hook` など設定で変わる場合は一致せず、比べる対象の一部で誤検出し得る）。④ デコレータの内側の関数が `functools.wraps` を使わないローカルのデコレータは、収集した実体が基準の位置と一致せず従来どおり検出される。⑤ 収集時検証（`--collect-only`）の構造的限界として、既存テストより前に挿入した `def test_0():` が実採点時だけ先に動いて既存テストの `__code__` を差し替える経路、および `@getattr(pytest, "fix"+"ture")(autouse=True)` が `_is_fixture()` を抜ける経路は検出できない。
- 追加の残り（独立レビュー）: デコレータの内側のラッパーを同じ qualname のまま本体だけ書き換える（例: `return None`）と、収集時の照合を通る。正当なデコレータ本体の修正と区別できないための取捨。

## PR 5 r8: Gemini r7 の指摘（対象 0da41ad）

| 指摘 | 対応 | テスト |
|---|---|---|
| #1(a) `_foreign_wrapper` 3 経路・#1(b) `co_flags`・#6 `_PureAddition` 3 点（ブロッキング） | `_foreign_wrapper` で `__code__` なし（`pos is None`）を拒否し、`_LIB_DIRS` 名除外を廃止。収集子プロセスが求めた stdlib/site-packages ルート配下に実在するファイル（`is_file()`）または基準の `known` のみ許可。`_COLLECT_SCRIPT` の `feed()` に `c.co_flags & 0x3FF` を追加。`_PureAddition` で `same()` の `ImportFrom` 拡張にも `_FIXTURE_NAMES` 別名禁止を適用し、モジュール直下の `FunctionDef`/`AsyncFunctionDef` に `self.shadow` チェックを追加、既存クラスへの追加時に同一ファイル内サブクラスの他基底クラス名（MRO）も `taken` に含める | `tests/test_review_r8.py`（偽パス exec・TMPDIR・partial・`__call__`・`.venv`/`__pycache__`/`site-packages` 名偽装・`CO_GENERATOR`/`CO_COROUTINE`・`ImportFrom` fixture 別名・モジュール直下 shadow・Mixin MRO・正当デコレータ非誤検出） |

## PR 5 r9: Gemini 全体レビュー round 1（対象 3479e11）

| 指摘 | 対応 | テスト | 残る穴・限界 |
|---|---|---|---|
| B1〜B6（ブロッキング全 6 件）・N2（正当デコレータ誤検出）・N4/N5 一部/N6 | B1: `lib_roots` 配下のラッパーも実体ファイルからコンパイルした `(co_firstlineno, digest)` 集合と照合（ファイル単位メモ化）、`__func__` アンラップを `types.MethodType` 限定に。N2: `functools._lru_cache_wrapper`（`gc.get_referents` で実体一致確認済みのみ）と基準の呼び出し可能クラス `__call__` を許可（`functools.partial`・新規クラス・`__wrapped__` 差し替え済み `lru_cache` は拒否）。B2: `_COLLECT_SCRIPT` 冒頭で他 import より前に `sys.path` から `""`/`"."`/`cwd` を除外（`PYTHONSAFEPATH=1` 併用）、`lib_roots` をホスト側で算出、収集処理・結果出力を関数クロージャ＋stdin 経由ランダムマーカー付き fd 出力に閉じて `__main__` に露出しない。B3: ホスト側 `cheatproof.py` の全読込を `lstat` + `S_ISREG` + `O_NOFOLLOW | O_NONBLOCK` + 親ディレクトリ symlink 検査の共通ヘルパー化し `tamper_check` に `lexists` を使用。B4: `.py` テストファイル末尾追記でも `_pure_addition` を通し、既存クラスへの非テストメンバー・fixture・予約フック・shadow を拒否（モジュール末尾の無害な無装飾補助関数・単純代入のみ許可）。B5: 判定役フォールバックの `inherit_env` を `ROUTE_VARS[family]` に絞り、非 `in_place` 判定も Linux + `bwrap` 利用可能時は `sandbox_cmd` で隔離（`README.md` に bwrap なし環境の限界を追記）。B6: `_judge_over_budget` をロック内で判定し月間予算は `billing == "api"` のみ適用。N4: `_USAGE_LINE_MAX` パッチで実超過分岐を検証。N5 一部・N6: `_budget_warnings`・`README.md`・`guide.html`・`reference.html` の矛盾修正、`log_overflow` の `sandbox` 記録、`env` 大文字小文字無視、`_link_chain` 親→子順と境界チェック、`WNOWAIT` 確認、`credentials.lend` の `0o600` 作成、`prereg.json` 破損→`SpecError`、`doctor` の `in_place` 期限表示 | `tests/test_review_r9.py`（B1〜B6・N2・N5/N6・正当デコレータ/staticmethod/classmethod/IsolatedAsyncioTestCase 非誤検出）、`tests/test_review_r7_drain.py` | B2 構造的限界: 同一 Python プロセス内で収集コードを実行するため、solver が `gc.get_objects()` / `sys._getframe()` / `sys.settrace` / `ctypes` 等で `_run()` クロージャのローカル変数（ランダムマーカーや `c.items`）を探索・改ざんする攻撃は原理的に完全には防げない。B5: `bwrap` のない OS（macOS/Windows や `sandbox: off`）では非 `in_place` Codex 判定の `-s read-only` にホストファイル読み取り限界が残る |

### r9 で未対応・今後の課題（非ブロッキング）

- **N1 (`leak_check` の `own_glob` / プレフィックス境界 / 2 段 `../`)**: `allowed` 置換の終端境界（`/..` 続行拒否・試行番号プレフィックス `1` vs `10` 境界）と `own_glob` の `trials/<task>/<profile>/<trial>` 部分の `*` および全セグメント `*` 禁止（macOS/Windows の主防御のため早めに対応推奨）。
- **N3 (`_PureAddition` / `_baseline_test_positions` の継承・別名・新規クラス本体)**: 別名参照された基底クラス（`Alias = Base`）の `ancestors()` 追跡、継承メソッドと `TestCase` 孫クラスの `_baseline_test_positions` 収録、新規クラス本体での既存クラス・グローバル書き換え制限。
- **N5 残り（ドキュメント・結果レポートの網羅更新）**:
  - `docs/reference.html`: `command` グレーダーの `ro` キー、`--trust-judge-input`、`grader_isolation`、bwrap 可視範囲の現行仕様（`home/` と `workdir/` のみ）反映。
  - `docs/guide.html`・`docs/design.html`: AWS 認証 Bedrock 限定、除外理由（`usage_limit`/`leak`）・失敗分類（`test_tamper`/`log_overflow`）、新規 6 モジュール構成図、`design.html` §6（sandbox 不採用記述）・§11（Windows 未検証記述）の現行実装・CI との整合。
  - `docs/results/2026-11-19-sound-eval-defaults.md`・`tasks/todo.md`: 項目 `b` の `--no-llm` + `csv-summary-insight` 注記、テスト件数更新、過去チェックリストの整理。
