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
