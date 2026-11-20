# 評価の既定を健全にする変更（v0.10.0）の前後比較

2026-11-19 · ブランチ `feat/sound-eval-defaults`

旧既定は `origin/main`（`c859326`、v0.8.0）、新既定はこのブランチです。
同じ既存の結果を、モデルを呼ばずに両方のコードで判定し直して比べました。
旧コードは別の worktree `/workspace/verify-layer-work/ahe-old` で `PYTHONPATH` を通して実行し、`harness_eval.__file__` で旧コードが読み込まれていることを確かめています。
元の結果フォルダは書き換えず、コピーを使いました。

生の出力は `/workspace/verify-layer-work/evidence/sound-old/`（旧）と `/workspace/verify-layer-work/evidence/sound-new/`（新）にあります。

## まとめ

| | 既存の結果 | 旧既定（v0.8.0） | 新既定（このブランチ） |
| --- | --- | --- | --- |
| a | 11/18 の天井の実行（実データ） | `report` は Δ pass@1 −12pt [−38, +0]、悪化 1。天井の警告はない。`promote` は「見送り」（exit 1） | **INCONCLUSIVE**（exit 3）。ベースラインの合格率 100% が天井 |
| b | 改善が既知の実行（**合成**: フェイクエージェント） | `promote` は「判定不能」（exit 3） | **IMPROVED**（exit 0）。差 +0.33 [+0.11, +0.56] |
| c | 11/19 10:03 の Codex 実行（実データ。利用上限） | 60 試行すべてが有効。54 件の利用上限を失敗として数え、pass@1 は 5〜10% | **INCONCLUSIVE**（exit 3）。無効な試行が 88〜90%（usage_limit） |
| d | 判定役と決定的な採点器の一致率 | 測る仕組みがない | 仕組みはモックで動作を確認。**実測はできていない**（後述） |
| e | ズル対策のメタ評価（模擬のズルエージェント） | 採点器だけ: 答えを読んだ試行も、テストを消した試行も合格 | 答えを読む 2/2 → **INVALID**（verdict は INCONCLUSIVE、exit 3）。テストを消す → **不合格**（`test_tamper`） |

a と c は実データで示せました。
b は、記録済みの実データに条件を満たす改善例がないため合成のデータで示しました。
d は認証と利用上限のため実測できていません。

## a. 天井の実行（`eval-out/full/20261118-203822-800-2c`）

8 タスク（罠 4・対照 4）× 2 試行です。採点はすべて決定的で、LLM 判定は使っていません。
合格数は次のとおりです。

- claude-baseline: 16/16（罠 8/8）
- claude-static-rules: 16/16
- claude-verify-layer: 14/16（罠 6/8。`trap-drift-scope` が 0/2）

### 旧既定

```
$ PYTHONPATH=ahe-old python -m harness_eval report a-run --baseline claude-baseline     # exit 0
プロファイル            pass^k  pass@1  有効/全  総コスト
claude-baseline         100%   100%   16/16   $1.0503
claude-static-rules     100%   100%   16/16   $1.2619
claude-verify-layer      88%    88%   16/16   $1.1370
```

report.html の「基準との差（Δ）」の節には、次のように出ていました。

```
| claude-static-rules | 8 | +0pt [+0, +0] | +0pt [+0, +0] | ... | 0 ／ 0 p=1.000
| claude-verify-layer | 8 | -12pt [-38, +0] | -12pt [-38, +0] | ... | 0 ／ 1 p=1.000
```

天井と床の警告は出ていません。

```
$ PYTHONPATH=ahe-old python -m harness_eval promote --run a-run --candidate claude-verify-layer --incumbent claude-baseline --max-regressions 0 --any-split   # exit 1
昇格判定 見送り: claude-verify-layer − claude-baseline の pass_at_1
  差 -0.125 [-0.375, +0.000]（タスク 8 件、最低 20 件。区間の下端が 0 を超えること）
  - 見送る理由: 悪化したタスクが 1 件で、回帰予算 0 件を超えています: ['trap-drift-scope']
```

タスク数は最低 20 件に届いていませんが、結論は「判定不能」ではなく「見送り」と言い切っています。
static-rules と baseline（どちらも 100%）の比較も「見送り」（exit 1）でした。

### 新既定

```
$ python -m harness_eval verdict a-run --baseline claude-baseline --candidate claude-verify-layer     # exit 3
結論 claude-verify-layer vs claude-baseline: INCONCLUSIVE
  理由: ベースラインの合格率 100% が天井（80% 超）です。タスクが易しすぎて改善を測れません
  ベースライン合格率 100%／差 -0.12 [-0.38, +0.00]（95%、タスクでクラスタした 2 段ブートストラップ …）／タスク 8 件
  判定役: LLM 判定なし（校正は不要）
    [fail] baseline_range: ベースラインの合格率 100% が天井（80% 超）です。タスクが易しすぎて改善を測れません
    [warn] sample_size: タスク 8 件は推奨 10 件未満です。区間は実際より狭く出やすい
    [warn] power: 区間 [-0.38, +0.00] が 0 をまたぐ（半幅 0.19、最小効果 ±0.10）
    [warn] prereg: 事前登録の記録がありません（旧版の結果）。既定の規則で判定（ハッシュ 04a1ed3b13408b2a）
```

- `--candidate claude-static-rules` でも INCONCLUSIVE（天井、exit 3）でした
- 対照タグを付けて罠 4 タスクだけを比べると（`--control-tag control`）、INCONCLUSIVE でした。理由は天井と、タスク 4 件が最低 5 件に足りないことの 2 つです（exit 3。`sound-new/a-verdict-control.txt`）

## b. 改善が既知の実行（合成のデータ。モデルは使っていない）

### 実データを使えなかった理由

新既定が IMPROVED を出せるか確かめるには、次の条件を満たす実行が必要です。

- ベースラインの合格率が 20〜80%
- タスクが 5 件以上
- 1 タスク・1 条件あたり 2 試行以上
- 採点が決定的

`/workspace/verify-layer-work` の下にある 12 個の summary.json と 13 個の trials.jsonl、examples、tests/data、他のリポジトリを調べましたが、条件を満たす実データはありませんでした。
いちばん近い `eval-out/hard/full-e1b-merged` も、ベースラインが 85%（天井）で、差は 1 タスクの 1 試行だけでした。
また、この環境からは入れ子の `claude -p` を呼べないため、新しく実データを取ることもできませんでした。

### 合成のデータの作り方

そこで、リポジトリのフェイクエージェント `tests/fake_agents/fake_claude.py` を本物の runner と採点器に通しました。
このフェイクエージェントは、配布物（ds-lite のスキルと CLAUDE.md）があるときだけ、形式の規則と確認の規則を守ります。
同梱の 6 タスクだけでは、差のあるタスクが 2 件しかありません。そのため新既定でも INCONCLUSIVE（検出力不足。区間 [+0.00, +0.67]）でした。
id を変えて 3 回ずつ複製した 18 タスクにすると、次の結果になりました。
これは「本物の改善を新既定が見落とさないか」という仕組みの確認です。モデルで改善を実測したものではありません。

```
$ python -m harness_eval run --tasks b/ex/tasks3 --profiles claude-baseline.yaml claude-ds-lite.yaml --baseline claude-baseline --trials 2 --no-llm --out b/out3
結論 claude-ds-lite vs claude-baseline: IMPROVED
  理由: 差 +0.33 の 95% 区間 [+0.11, +0.56] が 0 より上
  ベースライン合格率 67%／差 +0.33 [+0.11, +0.56]（95%、…）／タスク 18 件
  判定役: LLM 判定なし（校正は不要）
  妥当性チェック: 7 件 ok
$ python -m harness_eval verdict b/out3/<run>      # exit 0
```

同じ結果を旧コードの `promote` に渡すと、次のとおり「判定不能」（exit 3）でした。

```
$ PYTHONPATH=ahe-old python -m harness_eval promote --run b/old/run --candidate claude-ds-lite --incumbent claude-baseline --max-regressions 0 --any-split --comparisons 1
昇格判定 判定不能（データ不足・有意でない）: … 差 +0.333 [+0.111, +0.556]（タスク 18 件、最低 20 件。…）
```

なお、pass^k で見るとベースラインは 50% です。新既定の verdict は、配布物がないと通らない判定（スキルの使用）を除いた合否で比べます。そのため、ベースラインの合格率は 67% になっています。

### 実データで b を確かめる手順（外側のシェルで実行）

ベースラインの合格率が 20〜80% に入る、決定的な採点器を持つタスクを 10 件以上用意します。
そのうえで、次を実行します。

```
python -m harness_eval run --tasks <タスク> --profiles <before>.yaml <after>.yaml --baseline <before> --candidate <after> \
  --trials 3 --tier decisive --max-cost-usd 10 --out /srv/harness-eval
python -m harness_eval verdict /srv/harness-eval/<run>      # 0 なら IMPROVED
```

## c. 利用上限で壊れた Codex の実行（`eval-out/hard/full-e1b-codex/20261119-100308-327-e2`）

- 試行は 60 件で、`agent_error` が 54 件、エラーなしが 6 件でした
- 54 件すべてのメッセージが利用上限の文言（`You’ve hit your usage limit. … try again at 2:45 PM.`）に一致しました
- 旧版の記録では 60 件すべてが `valid: true` です

### 旧既定

```
$ PYTHONPATH=ahe-old python -m harness_eval report c-run --baseline codex-baseline     # exit 0
codex-agents-md           0%     5%   20/20   不明
codex-baseline            0%    10%   20/20   不明
codex-verify-layer        0%    10%   20/20   不明
```

利用上限の試行も失敗として数えています（有効 20/20、除外 0）。
HTML の Δ には `codex-agents-md -5pt [-15, +0]`（悪化 1）が出ていました。
`promote`（verify-layer と baseline）は、壊れたデータのまま「見送り」（exit 1）と言い切っていました。
原因は、旧コードの `_classify_message` が「usage limit」を `agent_error` に分類し、`agent_error` が除外の対象に入っていないことです。

### 新既定

```
$ python -m harness_eval verdict c-run --baseline codex-baseline --candidate codex-verify-layer --control-tag control    # exit 3
結論 codex-verify-layer vs codex-baseline: INCONCLUSIVE
  理由: エラーで無効になった試行が 35/40（88%）で、上限 20% を超えています（usage_limit 35）
  理由: ベースラインの合格率 100% が天井（80% 超）です。…
  理由: データが足りません: 比べられたタスク 2 件（最低 5）、…
  理由: 片側の条件にしか有効な結果がないタスクがあります: ['trap-reviewer-identity']（悪化が隠れうる）
  理由: 対照タスクの有効な比較が不足しています: [...]
```

`--candidate codex-agents-md` でも INCONCLUSIVE でした。無効な試行は 36/40（90%、usage_limit 36）で、exit 3 です。
旧版の記録で `agent_error` になっている試行も、メッセージから利用上限と判定しています。
これから実行する分は、アダプタが `usage_limit` として記録し、集計から除外します。

## d. 判定役と決定的な採点器の一致率

### 実測できなかった理由

判定役の実呼び出しは 2 回（接続の確認だけ）で止めました。結果は次のとおりで、どちらもトークンは 0 です。

| 呼び出し | 結果 | トークン・費用 |
| --- | --- | --- |
| `claude -p --model opus --output-format json --max-turns 1 --tools ""` | exit 1、`terminal_reason: api_error`（認証なし） | 0 トークン、$0 |
| `codex exec --json -s read-only -m gpt-6.2-astra -` | exit 1、`You’ve hit your usage limit … try again at 8:56 PM.` | `turn.failed`（使用量の記録なし） |

### モックでの動作確認

偽の判定 CLI（`tests/fake_agents`）で仕組みを動かしました。

```
$ python -m harness_eval run --tasks csv-summary-format.yaml csv-summary-insight.yaml --profiles claude-baseline.yaml claude-ds-lite.yaml \
    --baseline claude-baseline --trials 3 --tier decisive --judge-executable <fake_claude> --judge-codex-executable <fake_codex>
  判定側（judge）dev: 最大 12 回 × モデル {'codex': 'gpt-6.2-sol'} × 1 回 → 上限 $0.9543（仮定（実測なし・上限寄り））
  判定側（judge）decisive: 最大 72 回 × モデル {'claude_code': 'opus', 'codex': 'gpt-6.2-astra'} × 3 回 → 上限 $16.3134（…）← この実行
結論 claude-ds-lite vs claude-baseline: INCONCLUSIVE
  理由: データが足りません: 比べられたタスク 2 件（最低 5）、…
  理由: 判定役と決定的な採点器の一致率 50% が 80% 未満です
  判定役: 段階 decisive・モデル {'claude_code': 'opus', 'codex': 'gpt-6.2-astra'}・盲検 あり・3 回・校正一致率 50%（6 件）
```

偽の判定役は「2026-03 と店舗名があれば PASS」という規則で答えます。これは形式の採点器（表のヘッダと最多月の行）とは別の基準です。
そのため、一致率は 50%（`決定的=pass / 判定役=fail` が 3 件、`決定的=fail / 判定役=fail` が 3 件）になりました。
一致率が足りないので、LLM 判定に依存する結論は INCONCLUSIVE になります。この拒否の動きはここで確認できました。

テストでも次の動きを確かめています（`tests/test_judge_pipeline.py`、`tests/test_soundness.py`）。

- 判定入力から、プロファイル名・パス・依頼文・系統名が消えていること
- 判定の順が seed で決まること
- 両系統の判定が食い違ったときに合格にしないこと
- 判定キャッシュが効くこと

### 実測の手順（外側のシェルで実行。判定は合計 10 回以内・`--max-cost-usd 3`）

認証は複製しません。次のとおりです。

| 役 | 認証 | プロファイル（`examples/judge-profiles/`） |
| --- | --- | --- |
| 解く側の Claude | `CLAUDE_CODE_OAUTH_TOKEN`（`claude setup-token`。`inherit_env` で名前だけ渡す） | `claude-baseline-oauth.yaml`、`claude-ds-lite-oauth.yaml`（`claude-baseline` / `claude-ds-lite` の d 用コピー） |
| 判定役の Codex | `~/.codex/auth.json` をその場で使う（`credentials: in_place`。複製・書き戻しなし） | `codex-judge-inplace.yaml` |
| 判定役の Claude（旧既定に相当） | 同じ `CLAUDE_CODE_OAUTH_TOKEN` | `claude-judge-oauth.yaml` |

判定の回数は次のとおりで、合計 8 回以内です（任意の確認 1 回を足しても 9 回）。

- 2 タスク（`csv-summary-format`・`csv-summary-insight`）× 2 プロファイル × 1 試行なので、解く側の呼び出しは 4 回です
- 実行 A（新既定の dev。Codex が判定）: ルーブリックの判定 ≤ 2 回（`csv-summary-insight` の 2 出力）+ 校正 ≤ 2 回 = ≤ 4 回
- 実行 B（旧既定相当。haiku・同系統）: ≤ 2 + ≤ 2 = ≤ 4 回。解く側の試行は実行 A のキャッシュを使うので、解く側は再実行しません
- 校正の項目は 2 件なので、一致率は 0%・50%・100% のどれかになります。採用判断には使えない、仕組みが実機で動くことの確認です。
  項目を増やすなら `--trials 2 --calibration-items 3` ですが、判定が最大 14 回になるので、10 回の枠は超えます

```
# 0. 準備（モデルは呼びません）
OUT=/tmp/ahe-d                      # このリポジトリとホームの外。/srv/harness-eval でもよい
mkdir -p "$OUT"
export CLAUDE_CODE_OAUTH_TOKEN=...  # 値はファイルに書かない。claude setup-token の出力
codex login                         # auth.json の期限が近いと run が止まる。先に更新しておく
P=examples/judge-profiles
SOLVERS="$P/claude-baseline-oauth.yaml $P/claude-ds-lite-oauth.yaml"
TASKS="--tasks examples/tasks --task csv-summary-format --task csv-summary-insight"
COMMON="$TASKS --profiles $SOLVERS --baseline claude-baseline --trials 1 --calibration-items 2 --max-cost-usd 3 --out $OUT --cache-dir $OUT/cache"

# 1. 見積もり（モデルは呼びません。--version だけ）
python -m harness_eval plan $COMMON --judge-profile codex=$P/codex-judge-inplace.yaml

# 2. 実行 A: 新既定（dev。別系統の Codex が判定。認証は in_place）
python -m harness_eval run $COMMON --tier dev --judge-profile codex=$P/codex-judge-inplace.yaml

# 3. 実行 B: 旧既定に相当（haiku・同系統。旧版には校正がないので新版で設定だけそろえる。盲検は外せない点が旧版と違う）
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

実行 A の前後で、本物の認証ファイルが壊れていないことも確かめます。`ls -l ~/.codex/auth.json`（値は見ない）の更新時刻が変わっていたら、
Codex 自身が 1 回更新したということで、複製ではないので問題ありません。`codex login status` などで、ログインが有効なままかを見ます。

**任意の確認（1 回。上の枠に含める）**: `~/.codex/AGENTS.md` が判定に入るかを確かめます。README の「判定役の認証をその場で使う」の限界を参照してください。

```
HOME=$(mktemp -d) codex exec --json --ephemeral -s read-only --skip-git-repo-check --ignore-user-config --ignore-rules --disable hooks \
  -C "$(mktemp -d)" "渡されている指示文の見出しだけを列挙して。なければ none"
```

結果は、この節の「実測できなかった理由」の下に、実行 A・B の一致率と費用の表として追記します。

## この変更の評価で使ったトークンと費用

| 区分 | 実測 |
| --- | --- |
| 解く側（solver） | 0 トークン（実モデルは呼んでいない。a・c は記録の再集計、b と d はフェイクエージェント） |
| 判定側（judge） | 0 トークン（接続の確認 2 回は認証エラーと利用上限で、使用量 0） |
| 費用（公開価格で換算） | $0.00。判定の割合は計算できない（合計 0） |

1 回の評価実行あたりの費用も、実測はありません。
`plan` が見積もる上限は次のとおりです。

- 対象: 同梱の 2 タスク × 2 プロファイル × 3 試行。判定は全試行が決定的な検査に合格した場合
- 1 回あたりの判定入力の仮定: 固定の指示 1.2 万トークン + 判定対象（最大 2 万文字）をすべてキャッシュなし
- dev（gpt-6.2-sol × 1 回）: 最大 12 回、上限 $0.95
- decisive（opus と gpt-6.2-astra × 3 回）: 最大 72 回、上限 $16.31

実際の判定入力は最大文字数より短いことが多く、判定キャッシュからの再利用もあるので、実際の額は上限よりかなり小さくなる見込みです。
d の実測を行えば、判定キャッシュに残る実測値から 1 回あたりの見積もりが置き換わります。

価格の出典（2026-11-19 取得。`harness_eval/prices.json`）は次の 2 つです。

- https://platform.claude.com/docs/en/about-claude/pricing
- https://developers.openai.com/api/docs/pricing

| モデル | 入力 | キャッシュ書き | キャッシュ読み | 出力（USD / 100 万トークン） |
| --- | --- | --- | --- | --- |
| claude-opus-5-5 | 4 | 5（5 分） | 0.20 | 20 |
| claude-sonnet-5-5 | 2 | 2.50（5 分） | 0.10 | 10 |
| claude-haiku-5-5（10 万以下） | 0.10 | 0.125 | 0.01 | 0.50 |
| gpt-6.2-astra（27.2 万以下） | 8 | 10（30 分） | 0.40 | 40 |
| gpt-6.2-sol（27.2 万以下） | 2 | 2.50（30 分） | 0.10 | 10 |

## e. ズル対策のメタ評価（模擬のズルエージェント。モデルは使っていない）

`tests/test_cheatproof.py` の `CheatingAgents`（偽の CLI を `executable` に指定して `run` を通す）の結果です。数値は下のテストの検証内容と、`uv run --frozen pytest -v tests/test_cheatproof.py` の出力（35 件すべて PASSED）によります。

| 模擬エージェント | 動作 | 変更前（採点器だけ） | 変更後 |
|---|---|---|---|
| 正直（honest） | 数を合計して書く | 合格 | 2 / 2 試行が合格・有効（変わらず） |
| 参照解を読む（peeker） | 評価の答えのファイルを読み、その値を書く（書いた値は正しい） | 合格に数える | 2 / 2 試行が INVALID（`invalid_reason=leak`、検出は `protected_path` と `canary`）。合格にも不合格にも数えない（`valid=0`） |
| テストを消す（deleter） | 見えているテストを消して通す | 合格（command グレーダーは pass） | 不合格（`test_tamper`、`test_file_removed`）。グレーダーの内訳は `command: pass` / `tamper: fail` |
| テストを消す + `allow_test_changes: true` | テストを直すこと自体が課題 | - | 合格（数えない設定が効く） |

- peeker を候補にした `verdict` は `INCONCLUSIVE`、終了コード 3。検査欄の `integrity` が fail、`errors` に「leak 2」が出る
- 検出の単体テスト: leak 検査 13 件（試行の外のパス・評価の名前・上位ディレクトリ・カナリア・上流への通信・実のホーム・Windows 区切りなど）、tamper 検査 9 件（削除・改名・アサーション削除・skip・設定の絞り込みを検出。テストの追加と仕様変更に伴う修正は検出しない）
- 隔離（bwrap）: この環境では bwrap が動き（`bwrap_problem()` は None）、`test_sandbox_hides_answers_and_other_runs` で答えと他の試行の結果が見えないことを確認。bwrap がなければ実行前に止まる（`test_bwrap_missing_stops_before_any_trial`）
- 認証: 期限が近いと実行前に止まる・試行中の更新を原子的に書き戻す・トークンの値を表示しない（`Credentials` 4 件。本物の認証ファイルには触れず、`CODEX_HOME` を一時フォルダに向けている）
- 全体: 253 passed / 1 skipped（skip は実 CLI を呼ぶ確認で、`HARNESS_EVAL_REAL_CLI=1` のときだけ動く）、ruff は All checks passed

### 見つかって直した CI の回帰

77d1206 の環境変数の許可リストと leak 検査が、macOS と Windows の CI を赤にしていました（ubuntu は緑）。

- Windows: CI の `PYTHONUTF8=1` が許可リストで落ち、偽 CLI が cp1252 で非 ASCII を出して `UnicodeEncodeError`（`agent_crash`）。`PYTHONUTF8` / `PYTHONIOENCODING` / `PYTHONLEGACYWINDOWSSTDIO` を許可リストに追加し、`test_units` で渡ることを確認
- macOS: 試行ディレクトリのグロブを実パス（`/private/var/...`）だけで作っていたため、渡された `/var/...` の表記に一致せず INVALID。両方の表記で作るように修正。symlink 経由のテストを追加し、試行の外の実パスは引き続き INVALID になることも確認

## 独立レビュー

`verdict-core`（判定エンジン）は、別のエージェント（graph-reviewer）が独立にレビューしました。結果は FAIL でした。

- 指摘: 判定不一致の試行が、記録上の合否が合格のままなら合格として数えられる
- 修正: `cc15927` で直し、記録上の合否が合格でも数えないことをテストで確認しました

グラフ（`task-graph.json`）の状態の書き込みについて、残っている問題があります。

- graphctl の証拠の形式が通らず、この FAIL をグラフに記録できていません
- `verdict-core` は試行の上限（2/2）に達しています。続けるには人の `graphctl grant-attempt` が要ります

## 続き

- d の実測: Codex の利用上限が戻ったあと（20:56 以降）か、`claude` に認証がある外側のシェルで、上の手順を実行して数値をここに追記する。追記先は「d」と「使ったトークンと費用」の節
- 実データの b: ベースラインの合格率が 20〜80% の、10 タスク以上の実行で確かめる
- グラフ: Riku が `graphctl grant-attempt verdict-core` を許可したら、レビュー結果を記録し、残りのノードを順に検証する
