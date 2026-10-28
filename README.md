# Agent Harness Eval

Claude Code と Codex の「ハーネス」（CLAUDE.md / AGENTS.md、Skills、Subagents、Hooks、MCP、モデル設定）を、
**同じタスク定義で、配布される形のまま実行して採点し、変更前後やハーネスなしと比較する**ための評価ツールです。

- 1 コマンドで「タスク × 設定プロファイル × 試行回数」を実行し、pass@1・pass^k・コスト・時間・失敗分類を出す
- 比較はタスク単位の 95% 区間つき。リリース判定は事前に決めたマージンで 通過／不通過／判定不能 の 3 値
- 各試行は一時ホーム・一時作業ディレクトリで実行し、普段の `~/.claude` / `~/.codex` には触らない
- 採点はコード判定が基本（独自スクリプトも可）。LLM 判定は補助
- 結果は 1 ファイルの HTML レポート（外部通信なし）と JSON に残る
- 依存は Python 3.10+ と PyYAML だけ。エージェントは各 CLI（`claude -p` / `codex exec --json`）を直接呼ぶ

設計書「Agent Harness Eval 設計書（Claude Code / Codex 対応）」の実装に、以前の評価基盤の試み（旧評価基盤、2026-08）の評価方法を統合したものです。
Promptfoo・BenchFlow などの外部エンジンは使いません。

> **図解ガイド**: [`docs/guide.html`](docs/guide.html) をブラウザで開くと、流れと仕組みを図で確認できます（1 ファイル・外部通信なし）。
> 主要な仕組みは README（テキスト。どのエディタでも読める）と図解ガイド（図。ブラウザ）の 2 形式で載せています。
> **設計記録**: [`docs/design.html`](docs/design.html) に、既存ツールとの比較・評価の方法・採らなかった案と理由・残るリスクをまとめています。

### 全体像

```
 あなたが用意するもの                    harness-eval                         出てくるもの
 ─────────────────                   ────────────                        ──────────
 タスク YAML    （何をさせて、どう採点するか）  ─┐
 プロファイル YAML（どのエージェントに、どの配布物）─┼─▶  タスク × プロファイル × 試行回数  ─▶  report.html（比較表・Δ・失敗分類）
 配布物         （CLAUDE.md・スキル…）          ─┤     を 1 試行ずつ隔離して実行・採点      summary.json（集計・実行環境の記録）
 フィクスチャ    （合成データ）                   ─┘                                          trials.jsonl（1 行 1 試行の記録）
```

### 用語

| 用語 | 意味 | 例 |
|---|---|---|
| タスク | エージェントにさせる仕事 1 つ。プロンプト・入力データ・採点方法を書いた YAML | 「sales.csv を月別に集計して」 |
| プロファイル | 「どのエージェントを、どの配布物で動かすか」の YAML | `claude-baseline`（配布物なし）、`claude-ds-lite`（CLAUDE.md + スキル） |
| 配布物（ハーネス） | 評価したい設定一式。プロファイルの `install` で一時ホームに展開する | `examples/harnesses/ds-lite/` |
| フィクスチャ | タスクの入力データ。試行ごとに作業ディレクトリへ複製（合成データのみ） | `examples/fixtures/sales_synthetic/` |
| 試行 | 1 タスク × 1 プロファイルを 1 回実行して採点したもの | `csv-summary-format / claude-ds-lite #0` |
| グレーダー | 試行を採点するルール 1 つ（ファイル内容・ツール呼び出し・独自スクリプト・LLM 判定など） | 「summary.md に表ヘッダがある」 |
| ベースライン | 比較の基準にするプロファイル（通常は配布物なし） | `--baseline claude-baseline` |

---

## 1. 導入

```bash
# zip を展開したフォルダで
pip install -r requirements.txt        # PyYAML だけ（社内は Artifactory 経由で）
python -m harness_eval --version
```

`pip install .` すると `harness-eval` コマンドとしても使えます（以降の例の `python -m harness_eval` と同じ）。

評価するエージェントの CLI が PATH にあること（`claude --version` / `codex --version`）。起動フラグと出力形式を実機で確認した版は Claude Code 2.1.304〜2.1.308（`hook_used` は 2.1.306）、Codex 0.163.0 です。これより古い版は `doctor` が知らせます（`doctor --live` で実際に動くか確かめてください）。
PATH にない場合はプロファイルの `executable:` で指定します。

## 2. 5 分で試す（同梱サンプル）

### 使う順番

```
 harvest ──▶ 人が確認 ──▶ validate（--selftest）──▶ doctor --live ──▶ plan ──▶ run（スモーク）──▶ run（フル）──▶ gate
  実際の依頼    架空化した    YAML の      隔離環境で        呼び出し回数   1 試行・コード     3 試行・LLM      前回と比べ
  から架空の    下書きを      誤りを検出    認証と配布物の     と概算費用を   判定のみ。普段     判定あり。       通過 0／不通過 1／
  タスク下書き  読んで直す    （費用 0。--selftest で採点を正解例で確認）    読み込みを確認     確認（費用 0） はここだけ回す    リリース前だけ    判定不能 3
  （§4.1）                                 （最小呼び出し 1 回/プロファイル）
```

| コマンド | いつ使う | エージェント呼び出し |
|---|---|---|
| `harvest` | タスクを用意するとき（自分の履歴から。履歴がなければ同梱タスク） | 依頼 1 件につき 1〜2 回（書き換え） |
| `validate` | YAML を書いた・直したとき（harvest の下書きは確認後に `--selftest` で採点も試す） | なし |
| `doctor` | 初回・端末や認証方式を変えたとき（CLI の版と、経路を変える環境変数の引き継ぎを確認） | なし |
| `doctor --live` | 同上（隔離環境で認証と配布物の読み込みを実際に確認） | プロファイルごとに 1 回（"OK" と返すだけ） |
| `plan` | `run` の前に規模と費用を確かめたいとき | なし |
| `run --tag smoke --no-llm` | 配布物を変えるたび | タスク × プロファイル × 1（変更のない組み合わせはキャッシュ） |
| `run --trials 3` | リリース前・モデル移行時 | タスク × プロファイル × 3 + LLM 判定 |
| `gate --margin <m>` | リリース判定（CI など） | なし |
| `report` / `calibrate` | レポートの作り直し／LLM 判定と人手ラベルの照合 | なし |

```bash
# ⓪ 自分のセッション履歴から、実際の依頼を架空化したタスクの下書きを作る（件数だけ見るなら --dry-run）
python -m harness_eval harvest --out my-tasks --limit 10
#   → my-tasks/tasks/*.yaml と checks/*.py を読んで確認してから使う。履歴がなければ同梱タスクが書き出される
python -m harness_eval validate --tasks my-tasks/tasks --profiles examples/profiles --selftest   # 確認後: 採点を正解例で試す

# ① 定義の検証（YAML の誤り・設計ルール違反の警告）
python -m harness_eval validate --tasks examples/tasks --profiles examples/profiles

# ② 環境検証：CLI の版と経路の変数（費用 0）→ 隔離した状態で認証と配布物の読み込み（最小の呼び出し 1 回ずつ）
python -m harness_eval doctor --profiles examples/profiles/claude-*.yaml
python -m harness_eval doctor --profiles examples/profiles/claude-*.yaml --live

# ③ 見積もり：何回エージェントを呼ぶか・キャッシュで何回省けるか・概算費用
python -m harness_eval plan --tasks examples/tasks --profiles examples/profiles/claude-*.yaml --tag smoke

# ④ スモーク評価（1 試行・LLM 判定なし）
python -m harness_eval run --tasks examples/tasks --profiles examples/profiles/claude-*.yaml \
    --tag smoke --no-llm --baseline claude-baseline

# ⑤ フル評価（3 試行・LLM 判定あり・費用上限つき）
python -m harness_eval run --tasks examples/tasks --profiles examples/profiles/claude-*.yaml \
    --trials 3 --baseline claude-baseline --max-cost-usd 5
```

結果は `--out`（既定 `results/`）の `<実行ID>/report.html` に出ます。`--out` とその上のフォルダに `CLAUDE.md`・`AGENTS.md`・`.mcp.json` や `.claude/`・`.codex/`・`.agents/` フォルダ があると `run` は止まります。ホーム（`~/.claude` など）やリポジトリの下は該当しやすいので、`--out` はそれらの外にしてください（§3.3）。

**サンプルの中身**（履歴がないときの標準タスクも兼ねる）: 合成の売上 CSV（`examples/fixtures`）を使う 6 タスクと、
小さな配布物 `examples/harnesses/ds-lite`（CLAUDE.md の出力形式ルール・「曖昧なら確認」ルール + 月別集計スキル）。
4 タスクには正解例（`examples/references/`）があり、`validate --selftest` で「採点が正解例で通り、何もしなければ落ちる」ことを確かめられます。
何もしなくても通る判定（入力ファイルの有無など）があれば `--selftest` が注意として表示します（壊してはいけないものを守る判定なら意図どおり）。

| タスク | 想定している実際の依頼 | 主に見ること |
|---|---|---|
| `request-extract` | 依頼文どおりの条件で SQL 抽出し、結果と SQL を納品 | 条件の取り違えで「動くが違うデータ」を返さないか |
| `request-ambiguous` | 「先月の売上データを抽出して」だけの曖昧な依頼 | 推測で作業せず、確認の質問を返すか |
| `csv-summary-format` | 月別集計のレポート | 配布物の形式ルール（常時指示）が効くか |
| `csv-summary-skill` / `arithmetic-no-skill` | 集計レポート／単純な質問 | スキルが必要なときだけ呼ばれるか |
| `csv-summary-insight` | 集計＋考察 | LLM 判定（フル評価用） |

ベースライン（ハーネスなし）と ds-lite を比べると、
形式ルールとスキル呼び出しの差が Δ として出ます。参考までに、開発時に Claude Code 2.1.304 + haiku で
`--tag smoke --tag skills --no-llm`（3 タスク × 1 試行）を実行した結果は次のとおりでした
（1 試行なので傾向の確認用。判断には 3 試行以上を使う）。

| プロファイル | pass@1 | 落ちた理由 | 費用 |
|---|---|---|---|
| claude-baseline（配布物なし） | 33%（3 タスク中 1） | 表ヘッダ・最多月の形式（`instruction_violation`）、スキル未使用（`skill_selection`） | $0.0087 |
| claude-ds-lite | 100% | — | $0.0093 |

同じく Claude Code 2.1.308 + haiku で `request-extract`・`request-ambiguous` を 2 試行ずつ実行した結果:
抽出はどちらも 2/2 で正解。曖昧な依頼では ds-lite（確認ルールあり）が 2/2 で確認の質問を返し、
ベースラインは 1/2（もう 1 回は質問せずに「該当データなし」と答えた）でした。

## 3. 仕組み

### 3.1 実行全体の流れ（`run`）

```
 ① 読み込み     タスク・プロファイルの YAML を検証（未知のキーはエラー）
      │
 ② 組み合わせ   プロファイル × タスク（targets・--tag・--task で絞る）× 試行回数 = 試行の一覧
      │         並べ方: 同じ（試行・タスク）の各プロファイルを続けて実行し、先に走るプロファイルを試行ごとに入れ替える（--seed）
 ③ 試行ごとに（--jobs で並列）
      │   ├─ キャッシュにある？ ──yes──▶ 過去の結果を再利用（エージェントは呼ばない）
      │   ├─ 費用の上限（--max-cost-usd・口座ごとの --monthly-budget-usd）に到達？ ─yes─▶ 未実行（budget_stop）
      │   └─ no ──▶ 1 試行を実行（3.2）──▶ レート制限・過負荷なら待って再試行（--retry-infra 回まで）
      │                                  └─▶ 有効な結果をキャッシュに保存
      │
 ④ 集計         pass@1 / pass^k / Δ と 95% 区間 / コスト / 失敗分類（除外・未実行は分母に入れない）
      │
 ⑤ 出力         results/<実行ID>/ に trials.jsonl（1 試行ずつ追記）・summary.json・report.html
```

### 3.2 1 試行の流れ

```
 runner            isolation（隔離）                  エージェント CLI             graders（採点）
 ──────            ──────────────                  ────────────               ──────────
 試行ディレクトリ作成 ─▶ home/・workdir/ を作る
                      フィクスチャを workdir/ に複製
                      環境変数を差し替え（3.3）
                      install で配布物を home/ に展開
 コマンド組み立て ───────────────────────────────▶  起動（cwd = workdir/、
                                                     プロンプトは標準入力）
                                                     ・stdout.jsonl に記録
                                                     ・timeout_s で子孫ごと停止
 出力をパース  ◀──────────────────────────────────  終了
 （ツール・スキル・サブエージェント・hook・費用・トークン・エラー種別 → 試行レコード）
 採点を依頼 ─────────────────────────────────────────────────────────────────▶ コード判定 → 全合格なら LLM 判定※
 合否・失敗タグ ◀──────────────────────────────────────────────────────────────  スコア
 後始末: home/ を必ず削除（例外・Ctrl-C でも）。workdir/ は --keep に従う
```
※ 配布物がないと通らない判定（スキル・サブエージェント・hook を使うことの要求）の不合格だけなら、LLM 判定を省きません（基準との比較用スコアに必要なため）。

### 3.3 隔離：何をどこへ向けるか

普段の設定（`~/.claude`・`~/.codex`）には触らず、試行ごとに空の一時ホームから始めます。
これは「どの設定を読み込むか」の隔離で、ファイルの読み書きを禁止するものではありません（Read や Bash を許可すれば絶対パスで外も読めます）。評価には合成データだけを使ってください。
`install` のコピー先は試行ディレクトリの中に限ります（相対パスは一時ホーム基準。外を指すと `harness_error`）。

**上のフォルダの指示は防げない**: 試行の作業ディレクトリは `--out` の下にあり、CLI はそこから上のフォルダの指示・スキル・ルール・サブエージェント・コマンドも読み込みます（一時ホームでは防げない。最終レビューで、上位の `CLAUDE.md`、`.claude/` の skills・rules・agents・commands、git ルートまでの `AGENTS.md` を実測）。配布物なしの基準にも入って比較が壊れるため、`run` は `CLAUDE.md`・`AGENTS.md`・`.mcp.json` や `.claude/`・`.codex/`・`.agents/` フォルダ が `--out` とその上にあれば止まり（`--allow-ancestor-instructions` で承知の上で実行すると結果とレポートに記録）、`doctor` は NG にします。`--out` はリポジトリやホームの外（例: macOS `/Users/Shared/harness-eval`、Windows `C:\harness-eval`）に置いてください。OS の一時フォルダは Codex が拒否するため使えません。

```
 普段の環境                              1 試行の中
 ─────────                              ─────────
 ~/.claude/  ← 触らない                 results/<実行ID>/trials/<タスク>/<プロファイル>/<n>/
 ~/.codex/   ← 触らない                   ├─ home/                    ← HOME・USERPROFILE（試行後に削除）
 ~/.aws/credentials ─ 参照だけ ─────▶     │   └─ .claude/ または .codex/ ← CLAUDE_CONFIG_DIR / CODEX_HOME
 ~/.claude/.credentials.json ─ copy_credentials: true のときだけ複製 ─▶ （同上、試行後に削除）
                                          ├─ workdir/                 ← エージェントの作業ディレクトリ（フィクスチャの複製）
                                          ├─ stdout.jsonl             ← エージェントの生出力
                                          └─ stderr.txt
```

| 環境変数 | 試行中の値 | 理由 |
|---|---|---|
| `HOME`・`USERPROFILE` | `home/` | ユーザー設定・スキルを読ませない（`isolate_home: false` で無効） |
| `CLAUDE_CONFIG_DIR` / `CODEX_HOME` | `home/.claude` / `home/.codex` | 配布物だけを置いた設定ディレクトリで起動する |
| `AWS_SHARED_CREDENTIALS_FILE`・`AWS_CONFIG_FILE` | 本物の `~/.aws/…` | ホームを差し替えても Bedrock の認証を使えるように（未設定でファイルがあるときだけ） |
| `CLAUDECODE` など親セッションの変数 | 削除 | Claude Code の中から実行しても入れ子扱いにならないように |
| プロファイルの `env` | 追加・上書き | Bedrock 設定など |
| 経路を変える変数（API キー・Bedrock の切り替え・モデルの別名など） | 親の値をそのまま渡す | `env` か `inherit_env`（名前だけ）で宣言する。宣言のない引き継ぎは `doctor` が NG（§5「認証」） |

Claude Code にはさらに `--settings {"syncClaudeAiSkills":false,"syncClaudeAiPlugins":false}` を渡し、claude.ai アカウントから同期されるスキル・プラグインの混入を防ぎます。
それでも残るもの（同梱スキル・組織の管理設定。上のフォルダの指示は上記のとおり止める）があるため、実際に読み込まれた構成要素を試行ごとに記録します（§10）。

### 3.4 モジュール構成

```
 cli.py ──▶ runner.py ──┬──▶ spec.py        YAML → Task / Profile
   │                    ├──▶ isolation.py   一時ホーム・環境変数・install・プロセス実行
   │                    ├──▶ adapters.py    CLI ごとのコマンドと出力パース（Claude Code / Codex）
   │                    ├──▶ graders.py     採点（isolation.fill で置換）
   │                    ├──▶ record.py      エージェント非依存の試行レコード
   │                    └──▶ metrics.py     集計・Δ・ゲート
   ├──▶ report.py ──▶ metrics.py            HTML レポート
   ├──▶ metrics.py                          gate（前回との比較）
   └──▶ harvest.py ──▶ isolation / graders  履歴 → 架空化したタスクの下書き
```

| ファイル | 役割 |
|---|---|
| `harness_eval/spec.py` | タスク・プロファイル YAML の読み込みと検証（未知のキーはエラー） |
| `harness_eval/isolation.py` | 一時ホーム・環境変数・install・プロセス実行（タイムアウト時は子孫プロセスも停止） |
| `harness_eval/adapters.py` | CLI ごとのコマンド組み立てと出力パース（Claude Code / Codex） |
| `harness_eval/record.py` | エージェント非依存の試行レコード |
| `harness_eval/graders.py` | グレーダー |
| `harness_eval/metrics.py` | 指標・Δ・リリースゲート |
| `harness_eval/runner.py` | 実行の司令塔（キャッシュ・再試行・予算・並列） |
| `harness_eval/report.py` | HTML レポート |
| `harness_eval/harvest.py` | セッション履歴から実際の依頼を集め、架空化したタスクの下書きを作る |
| `harness_eval/cli.py` | コマンド |

## 4. タスクの書き方

```yaml
id: csv-summary-format              # 英数字・_ . -
description: 月別集計で配布ルールの出力形式が守られるか
targets: [claude_code, codex]       # 実行するエージェント
tags: [instructions, smoke]         # --tag で絞り込む
fixture: ../fixtures/sales_synthetic   # 試行ごとに作業ディレクトリへ複製（ファイル1つでも可）
prompt: |
  sales.csv を月別に集計して summary.md にまとめてください。
limits: {max_turns: 15, timeout_s: 300, max_budget_usd: 0.5}
allow_tools: [Read, Write, Edit, Bash, Skill]   # Claude Code の --allowedTools（-p では承認が必要なツールは許可しないと拒否される）
deny_tools: []                                  # Claude Code の --disallowedTools
paired_negative: arithmetic-no-skill            # 対になる「してはいけない」タスク（任意）
graders:
  - type: command
    run: '{python} {task_dir}/../checks/check_monthly_totals.py {task_dir}/../fixtures/sales_synthetic/sales.csv'
    weight: 2
  - type: file_contains
    path: summary.md
    pattern: '^\|\s*月\s*\|\s*売上合計\s*\|'
    category: instruction_violation
  - type: tool_used
    tool: Bash|shell
    input_match: 'rm\s+-rf'
    max: 0
```

相対パスはタスクファイルの場所が基準です。

### グレーダー

| type | 見るもの | 主なキー | 種別 |
|---|---|---|---|
| `file_exists` | 作業ディレクトリのファイルの有無 | `path` | 結果 |
| `file_contains` | ファイル内容の正規表現（複数行モード） | `path`, `pattern` | 結果 |
| `regex` | 最終応答の正規表現 | `pattern` | 結果 |
| `command` | 独自スクリプト。終了コード 0 で合格 | `run`, `timeout_s` | 結果 |
| `llm` | LLM 判定（PASS / FAIL / UNKNOWN） | `criteria`, `focus`（`final` かファイルパス）, `max_chars` | 結果 |
| `tool_used` | ツール呼び出しの回数 | `tool`（名前の正規表現・完全一致）, `input_match`, `min`, `max`, `include_subagents` | 過程 |
| `skill_used` | スキル呼び出しの回数 | `skill`, `min`, `max` | 過程 |
| `subagent_used` | サブエージェント呼び出しの回数 | `name`, `min`, `max` | 過程 |
| `hook_used` | hook の実行回数（Claude Code のみ） | `hook`（`イベント:マッチャー` の正規表現・完全一致）, `outcome`, `min`, `max` | 過程 |

全グレーダー共通: `weight`（既定 1）、`negate`（判定を反転）、`category`（失敗分類のタグ）、`description`。
`min`/`max` を省略すると「1 回以上」、`max` だけ書くと「0〜max 回」。
`skill_used`・`subagent_used`・`hook_used` で 1 回以上を求める判定は「配布物がないと通らない判定」として、配布物なしの基準と比べる Δ では
両側とも除きます（試行の合否・スコアには入ります）。

### 採点の流れと試行の扱い

```
 エージェント終了
   │
   ├─ 環境起因の異常終了？（rate_limit・overloaded・auth・api_error・network・launch_failed・harness_error・interrupted）
   │      └─ yes ─▶【除外】採点しない・集計に入れない（rate_limit / overloaded は再試行）
   │
   ├─ コード判定（llm 以外のグレーダー）をすべて実行
   │
   ├─ LLM 判定（llm グレーダー）
   │      ├─ --no-llm ─────────────────────▶ not_run（スコアの分母から外す）
   │      ├─ コード判定に不合格 or 異常終了あり ─▶ skipped_prior_fail（不合格扱い。判定は呼ばない＝節約）
   │      │   （配布物がないと通らない判定だけの不合格では省かない。基準との比較用スコアに必要なため）
   │      ├─ 判定を呼べない（認証など）─────▶【除外】judge_error
   │      └─ 実行 ─▶ PASS / FAIL / UNKNOWN（UNKNOWN は不合格扱い）
   │
   ├─ 実行されたグレーダーが 0 ─▶【除外】not_graded
   │
   └─ スコア = 合格したグレーダーの重みの合計 ÷ 実行したグレーダーの重みの合計（not_run・skipped_prior_fail は分母に入れない）
          ├─ スコア 1.0 かつ異常終了なし ─▶【合格】
          └─ それ以外 ─▶【不合格】失敗タグ = 異常終了の種類 ＋ 落ちたグレーダーの category
```

| 試行の状態 | 例 | pass@1・pass^k の分母 | キャッシュ |
|---|---|---|---|
| 合格 | 全グレーダー合格 | 入る | 保存 |
| 不合格 | 形式違反、`timeout`、`max_turns`、`budget_exceeded`、`agent_error` | 入る | 保存（`timeout` だけは保存しない） |
| 除外 | `rate_limit`、`auth`、`api_error`、`network`、`judge_error`、`not_graded`、`interrupted` など | 入らない（件数と理由をレポートに表示） | 保存しない |
| 未実行 | `--max-cost-usd`・`--monthly-budget-usd` に到達（`budget_stop`） | 入らない（件数をレポートに表示） | 保存しない |

- `command` の `run` では `{python}`（実行中の Python）・`{workdir}`・`{task_dir}` が使えます。作業ディレクトリで実行され、
  環境変数 `TRIAL_WORKDIR` と `TRIAL_RECORD`（試行レコード JSON のパス）が渡ります。
  **置換した値はシェル用に自動で引用されるので、YAML 側で `"{task_dir}"` のように囲まないでください**（空白を含むパスで壊れます）。
- 正解は**元のフィクスチャ**から計算してください。作業ディレクトリのファイルはエージェントが書き換えられます
  （同梱の採点スクリプトは元の sales.csv を引数で受け取ります）。
  スクリプトは「形式」と「正しさ」を分け、形式の違いで正しさを落とさないようにしてください
  （同梱の `check_monthly_totals.py` は初版でまさにこれを誤り、実出力で発見して直しました。回帰テスト `tests/data/summaries` 参照）。
- ツール名はエージェントで異なります。Claude Code は `Bash`・`Write`・`Edit`・`Skill`・`Agent` など、
  Codex は `shell`（コマンド実行）・`file_change`・`web_search`・`collab:<操作>`。MCP はどちらも `mcp__<サーバー>__<ツール>`。
  両方で同じタスクを使うなら `tool: Bash|shell` のように両方を書きます。
- **Codex のスキル検出は推定**です（SKILL.md を読むシェルコマンドから名前を拾う）。Codex では成果物を見るグレーダーを主にしてください。
- `hook_used` は Claude Code の `--include-hook-events` の記録を数えます（このフラグは `hook_used` を使うタスクにだけ付けます）。`hook` は `PreToolUse:Bash`・`SessionStart:startup` のような
  「イベント:マッチャー」の名前、`outcome` は実測で `success`（成功）・`error`（終了コード 2 などでブロック・失敗）。
  Codex は hooks を実行しても `--json` に記録を出さないため、Codex の試行では `not_run`（スコアの分母から外す）になります。
  例（2.1.306 で実際に通した設定）: 配布物の hook が `rm` をブロックしたことを確かめる

  ```yaml
  graders:
    - {type: file_exists, path: a.txt, description: a.txt が残っている}
    - {type: hook_used, hook: "PreToolUse:Bash", outcome: error, description: hook が rm をブロックした}
  ```

### 設計ルール（最後の 1 つ以外は validate が警告）
- 実行される部品（install の `run`、hooks・MCP を定義しうる設定ファイル、`--mcp-config` などの追加引数）を含むプロファイルには警告が出ます。利用者の権限で動き sandbox はないため、社内でレビュー済みの配布物だけを評価してください
- 各タスクに「結果を見るグレーダー」と「過程を見るグレーダー」を最低 1 つずつ
- スキル／サブエージェントを「使うべき」タスクには、`paired_negative` で「使うべきでない」タスクを対にする
- ハーネスなしのベースラインプロファイルを 1 つ用意する
- タスクのデータは合成フィクスチャだけを使う（社内データを評価に流さない）

### 4.1 実際の依頼からタスクを作る（`harvest`）

評価は「ユーザーが実際に頼む仕事」で行うのがいちばん確かです。`harvest` は自分のセッション履歴から実際の依頼を集め、
**同じ種類・同じ難しさのまま、名前・数値・データをすべて架空に置き換えた**タスクの下書きを作ります。
元の依頼をそのまま実行したり、本物のデータや環境に触れたりはしません。

```
 ~/.claude/history.jsonl ─┐                              メモリ上だけ（ファイル・ログに書かない）
 ~/.claude/projects/*/*.jsonl ┼─▶ ① 人が打った依頼を取り出す ─▶ ② 各セッションの最初の依頼を
 ~/.codex/history.jsonl ──┤      （ツール結果・通知・/コマンド・     新しい順に --limit 件
 ~/.codex/sessions/**/rollout-*.jsonl ┘  20 文字未満は除く。貼り付けは展開）
                                                  │
                                                  ▼
 ③ Claude Code で書き換え（一時ホーム・ツールなし・会話を保存しない・構造化出力）
    固有名詞・数値・日付・表名を架空に → 合成データ・採点スクリプト・正解例を作る
                                                  │
                                                  ▼
 ④ 自動検査 ── 元の識別子・名前・年月・数値・25 文字以上の同じ文が残っている ──▶ 1 回だけ作り直し → それでも残れば見送り
             ── 採点スクリプトが Python として読めない／結果を見る採点がない ──▶ 見送り
             ── ファイル・正規表現の判定が正解例で通らない／何もしなくても通る ──▶ 見送り
             （LLM が書いた採点スクリプトは、ここでは実行しない）
             ── 依頼文で求めていない判定 ──▶ その判定だけ外す
                                                  │
                                                  ▼
 ⑤ 書き出し: --out/tasks/h-*.yaml・fixtures/h-*/・checks/h-*.py・references/h-*（正解例）
                                                  │
                                                  ▼
 ⑥ 人が確認 ─▶ validate --selftest（採点スクリプトを実行し、正解例で通り・何もしなければ落ちるか）─▶ run
    （履歴が 1 件もなければ、同梱の標準タスク 6 件を書き出す。既にあるファイルは上書きしない）
```

| 読む履歴 | 中身（実機・ソースで確認） | 取り出すもの |
|---|---|---|
| Claude Code `history.jsonl` | 入力欄に打った文。貼り付けは `[Pasted text #1 +N lines]` と `pastedContents`（2.1.308） | 貼り付けを展開した依頼文 |
| Claude Code `projects/*/*.jsonl` | 会話の記録。人が打った文は `turnOrigin: "human"`（2.1.308） | ツール結果・通知・メタ情報を除いた依頼文 |
| Codex `history.jsonl` | `{"session_id","ts","text"}`。既定で保存（0.163.0 のソース） | `text` |
| Codex `sessions/**/rollout-*.jsonl` | `event_msg` の `user_message`（ソース） | `message` |

`CLAUDE_CONFIG_DIR`・`CODEX_HOME` を設定していればその場所を読みます。チームの履歴をまとめて使う場合は `--source` に
フォルダか履歴ファイルを並べます。

**守っていること**

| 心配 | 対策 |
|---|---|
| 依頼文の社外秘が残る | 書き換え指示で固有名詞・数値・日付・表名などを架空化。元の依頼にある識別子（`db.table`・snake_case・ファイル名）・URL・メール・大文字の名前（`NOVA` など）・年月・4 桁以上の数値（4 桁は合成データの値との偶然の一致を除く）と、25 文字以上の同じ文が残った案は、残った値を具体的に伝えて 1 回作り直し、それでも残れば見送り（全角半角・大文字小文字・改行・年月の書き方の違いは無視）。**日本語の固有名詞（会社名・人名）は機械では判定できないため、人の確認が必須** |
| 依頼文がファイルやログに残る | 元の依頼文はメモリ上だけで扱い、表示も件数だけ。書き換えの呼び出しは OS の一時フォルダの一時ホーム・`--no-session-persistence` で、新しい履歴を作らない。テレメトリのプロンプト記録（`OTEL_LOG_USER_PROMPTS`・`OTEL_LOG_TOOL_DETAILS`）の環境変数は外して呼ぶ（組織の管理設定で有効にされている場合は外せない） |
| LLM が書いた採点スクリプト | `harvest` は実行しない（構文の確認だけ）。Python のコードの安全性を静的に確かめるのは確実にできない（レビューで、許可した標準モジュールだけでもコマンド実行や通信ができる書き方が見つかった）ため。実行するのは、人が中身を読んだあとの `validate --selftest` と `run` |
| 設定ファイルを紛れ込ませる | 合成データの置き場所に `.` で始まるもの（`.claude/`・`.mcp.json`）と `CLAUDE.md`・`CLAUDE.local.md`・`AGENTS.md`・`AGENTS.override.md` は置けない |
| 本物の環境で実行してしまう | 合成データだけで完結する形に書き換える。Claude Code では `ssh`・`scp`・`curl`・`wget`・`trino`・`hive`・`beeline`・`psql`・`mysql`・WebFetch・WebSearch を禁止（`Bash(ssh *)` 形式の禁止が効くことを実機で確認）。ただし Bash 自体は許可するので、Python からの通信などまでは防げない（最善の努力）。Codex にはこの禁止が効かず、`codex_sandbox: workspace-write`（既定でネットワーク不可。0.163.0 の設定定義で確認）で制限する。Bedrock 認証のため本物の AWS 認証ファイルは参照できる状態なので、合成データ以外を置かないこと |
| 採点が不公平 | 正解例で通り・何もしなければ落ちることを自動確認。依頼で求めていない判定（例: 最終応答に特定の店名）は外す |
| 接続を試みても気づけない | 生成したタスクには「本物の環境（ssh・DB・Web）に接続しようとしていない」という過程の判定を必ず付ける（禁止で止められた呼び出しも記録に残るので、試みも数える） |
| 未ログインのまま全件失敗 | 認証エラーなら最初の 1 件で止める（`--profile` で認証を借りるプロファイルを指定） |
| 費用 | 書き換えは依頼 1 件に 1〜2 回。開発時の実測は sonnet で 1 回あたり約 $0.06〜0.15（合成の依頼 3 件 × 2 回の試行で計 7 回）。費用は台帳（`--ledger`）に記録 |

書き換えた依頼文は、元の依頼と同じく Claude（社内なら Bedrock）に送られます。`--profile` で認証を借りるプロファイルを指定できます。

**確認するときの観点**: ① 会社名・人名・社内システム名が残っていないか ② 依頼の性質（難しさ・曖昧さ）が元と同じか
③ 採点スクリプトが「正しいが書き方の違う答え」を落とさないか（開発時の実測で、改善 SQL の書き方を 1 通りに決め打ちする採点スクリプトが出た）。
直したら `validate --selftest` → `run` で使います。開発時に合成の依頼 3 件で試した下書きは、3 件とも元の名前・識別子が残らず、
haiku（配布物なし）で 3/3 解けました。

**限界**: 1 回の指示で完結する評価なので、確認の質問に人が答えて続く会話全体は再現できません（「確認すべき場面で確認したか」までを見ます）。
各セッションの最初の依頼だけを使うため、途中で出た別の依頼は拾いません。

## 5. プロファイルの書き方

```yaml
id: claude-ds-lite
agent: claude_code            # claude_code / codex
model: sonnet                 # --model / -m に渡す（省略で CLI の既定）
install:                      # 一時ホームへの配布物の展開（上から順に実行）
  - copy: ../harnesses/ds-lite/CLAUDE.md
    to: "{config_dir}/CLAUDE.md"
  - copy: ../harnesses/ds-lite/skills
    to: "{config_dir}/skills"
  - run: '{python} path/to/installer.py --profile ds --target {config_dir}'   # 社内インストーラをそのまま使う（プロファイルのフォルダで実行）
env:                          # 追加の環境変数（{…} の置換可）。値を固定してよいものだけ
  CLAUDE_CODE_USE_BEDROCK: "1"
  AWS_REGION: ap-northeast-1
inherit_env: []               # 親の環境の値を使う経路の変数（名前だけ。例: [ANTHROPIC_API_KEY]。値は書かない）
env_unset: []                 # 消したい環境変数
billing: api                  # api（呼び出しごとに課金。月の予算に数える）/ subscription（定額。月の予算に数えない）
extra_args: []                # CLI に足す引数（{…} の置換可。例: [--mcp-config, "{config_dir}/mcp.json"]）
permission_mode: null         # Claude Code の --permission-mode
codex_sandbox: workspace-write
pricing: null                 # Codex の概算費用用（後述）
hash_paths: []                # run で使う配布元など、変更検知（キャッシュ判定）に含めるパス
isolate_home: true            # HOME / USERPROFILE を一時ホームに向ける
isolation_settings: true      # Claude Code に --settings {"syncClaudeAiSkills":false,"syncClaudeAiPlugins":false}
copy_credentials: false       # 本物の認証ファイルを一時ホームに複製（試行後に削除）
executable: null              # CLI の場所（例: ["C:/tools/claude.exe"]）
```

置換できるもの: `{home}`（一時ホーム）・`{config_dir}`（Claude Code は `{home}/.claude`、Codex は `{home}/.codex`）・
`{workdir}`・`{profile_dir}`・`{python}`。`run` の中では値が自動で引用されるので、引用符で囲まないでください。

**ベースライン**は `install` を空にしたプロファイルです（空の設定ディレクトリで起動）。

### 認証
一時ホームを使うため、ログイン情報が普段の場所にある方式はそのままでは使えません。
- **経路を変える変数は宣言する**: API キー・トークン・Bedrock などの切り替え・接続先・モデルの別名の実体を変える環境変数
  （`ANTHROPIC_API_KEY`・`CLAUDE_CODE_OAUTH_TOKEN`・`CLAUDE_CODE_USE_BEDROCK`・`AWS_BEARER_TOKEN_BEDROCK`・`ANTHROPIC_DEFAULT_SONNET_MODEL`・
  `CODEX_API_KEY` など。一覧は `harness_eval/spec.py` の `ROUTE_VARS`）は、試行の経路と請求先を変えます。値を固定してよいものは `env` に書き、
  親の環境の値を使うもの（秘密の値）は **`inherit_env: [ANTHROPIC_API_KEY]` のように名前だけ**を書きます（値はプロファイルに書かない）。
  宣言せずに親から引き継ぐと `doctor` が NG（終了コード 1）、`run` が警告します。経路の変数はキャッシュキーに入るため（キー・トークン・ヘッダは名前だけ、それ以外は値のハッシュも）、
  経路を変えると再実行されます（値は記録しません）。
- **Bedrock（Claude Code）**: `CLAUDE_CODE_USE_BEDROCK` などを `env`（または `inherit_env`）で。AWS の認証ファイルは、
  ホームを差し替えても本物（`~/.aws/credentials` / `~/.aws/config`）を参照するよう `AWS_SHARED_CREDENTIALS_FILE` / `AWS_CONFIG_FILE` を自動で設定します（未設定でファイルがある場合）。
- **ファイルに保存されたログイン**（`~/.codex/auth.json`、Linux の `~/.claude/.credentials.json`）: `copy_credentials: true`。
  **macOS のキーチェーンに保存されたログイン（`claude auth login`）は試行では使えません**（2026-10-28、Claude Code 2.1.295 で実測。
  一時ホームでも本物のホームでも、設定フォルダ `CLAUDE_CONFIG_DIR` を分けると「Not logged in」になる）。
- **サブスクリプション（Pro / Max）で評価する**: 端末で `claude setup-token` を実行して長期トークンを作り（推論専用）、
  `export CLAUDE_CODE_OAUTH_TOKEN=<トークン>` してから実行します。プロファイルには値を書かず、`inherit_env: [CLAUDE_CODE_OAUTH_TOKEN]` と
  `billing: subscription` を書きます（CLI が表示する使い方と同じ。トークンはチャットやファイルに貼らない）。
- **LLM 判定の認証**: 判定も一時ホームで動くため、プロファイルの `env`・`env_unset`・`copy_credentials`・`executable` を借ります
  （配布物と追加引数は借りません）。借りる先は `--judge-profile` で指定、省略時は判定エージェントと同じ種類の `--baseline`、
  なければ最初の同種プロファイル。判定が認証エラーなどで動かなかった試行は `judge_error` として集計から除外します。

### Codex 用の配置
Codex 公式ドキュメントの探索場所に合わせます: 常時指示は `CODEX_HOME/AGENTS.md`、ユーザースキルは `~/.agents/skills`
（`CODEX_HOME` 配下ではない）、プロジェクトのスキルは作業ディレクトリの `.agents/skills`。
SKILL.md は共通形式なのでそのまま複製できます（`examples/profiles/codex-ds-lite.yaml`）。
サブエージェント定義（`.codex/agents/*.toml`）への変換機能はありません。

**Codex の hooks**: `CODEX_HOME/hooks.json` に置いた hooks は、一時ホームでは「未信頼」のため `codex exec` では**警告なしで実行されません**
（0.163.0 で実測。`--dangerously-bypass-hook-trust` を付けると実行された）。普段の利用では一度信頼すれば実行されるので、
その状態を再現するため、Codex の試行には常にこのフラグを付けます。一時ホームに入るのはプロファイルの `install` で置いたものだけです。

**Codex で効かない項目**: タスクの `max_turns`・`max_budget_usd`・`allow_tools`・`deny_tools` とプロファイルの `permission_mode` は Claude Code 用で、Codex には対応するフラグがないため使われません（Codex の試行は `timeout_s` と `codex_sandbox` で制限します）。Codex はターン数を出さないため、レポートの平均ターンは空欄です。

## 6. 指標

| 指標 | 定義 |
|---|---|
| 試行スコア | 合格したグレーダーの重み付き割合（実行しなかった判定 `not_run`・`skipped_prior_fail` は分母から除外） |
| 試行の合格 | スコア 1.0 かつ失敗扱いの異常終了なし |
| pass@1 | タスクごとに「合格した有効試行 ÷ 有効試行」、全体はタスク平均 |
| pass^k | タスクごとに k 回続けて合格する確率。有効試行 n・合格 c なら C(c,k)/C(n,k)。n = k なら「全試行合格で 1、それ以外 0」。n < k のタスクは算出しない（レポートに算出できたタスク数を表示）。全体はタスク平均。**一貫性が重要な配布ツールでは主指標** |
| Δ | 比較プロファイル − 基準プロファイル（両方で値があるタスクだけ）。配布物がないと通らない判定を両側とも除いた値で計算し、タスク単位のブートストラップによる 95% 区間を付ける（区間が 0 をまたげば差があるとは言えない）。v0.3.0 以前の結果では除けないため全判定の値で計算し、レポートに注意を出す |
| 合格あたりのコスト | 総コスト ÷ 合格試行数。コスト不明の試行があれば出さない（0 で埋めない） |
| 失敗分類 | 不合格の試行に付くタグ。異常終了の種類（`timeout`・`max_turns`・`budget_exceeded`・`agent_error`・`agent_crash`）と、不合格グレーダーの `category`（既定: `wrong_output`・`process_violation`・`skill_selection`・`delegation`・`hook`） |

**計算例**（`--trials 3`、k = 3 と k = 2 の場合）

| タスク | 3 試行の結果 | pass@1 | pass^3 | pass^2 = C(c,2)/C(3,2) |
|---|---|---|---|---|
| A | ✓ ✓ ✓（c = 3） | 3/3 = 100% | 1 | 3/3 = 1 |
| B | ✓ ✗ ✓（c = 2） | 2/3 = 67% | 0 | 1/3 = 0.33 |
| C | ✗ ✗ ✓（c = 1） | 1/3 = 33% | 0 | 0/3 = 0 |
| プロファイル全体（タスク平均） | | 67% | 0.33 | 0.44 |

pass@1 は「平均的にどれくらいできるか」、pass^k は「k 回続けて使っても毎回できるか」。配布物の変更でたまにしか失敗しなくなった、
という差は pass@1 では小さく見え、pass^k で大きく出ます。

**集計から除外する試行**（評価対象の失敗ではなく環境起因など）: `rate_limit`・`overloaded`・`auth`・`api_error`（それ以外の API エラー。例: モデル ID の誤りで 400）・`launch_failed`・`harness_error`・
`network`（Codex が API に接続できず再接続を繰り返しただけ）・`judge_error`（LLM 判定を呼べなかった）・
`not_graded`（実行されたグレーダーがない。例: `llm` だけのタスクを `--no-llm` で実行）・`interrupted`（Ctrl-C で止めた）。
レポートに件数と理由を出します。費用の上限に達して実行しなかった試行（`budget_stop`）は「未実行」として件数だけ出します。
タイムアウト・ターン上限・予算超過は「失敗」として数えます。

**費用**: エージェントと LLM 判定の合計。どちらかが不明な試行が 1 つでもあれば総額は「不明」と表示します。
試行レコードの `cost_source` が出どころです。Claude Code は `client_estimate`（CLI が API 単価で計算した `total_cost_usd`。請求額とは異なりうる。
サブスクリプションでは呼び出しごとの請求はない）。Codex は CLI が費用を出さないため、`pricing`（100 万トークンあたりの単価）を書いたときだけ
`token_estimate` として概算します（なければ不明）。
計算式は「(input − cached) × input単価 + cached × cached単価 + output × output単価」で、
OpenAI API の usage 定義（cached は input の内数）を前提にしています。社内の請求と照合してから使ってください。

## 7. トークンを無駄にしない運用

評価の正しさを損なわない節約だけを組み込んでいます。

```
 トークンを使う場所            使わずに済ませる仕組み                         評価の正しさへの影響
 ───────────────            ─────────────────                         ──────────────
 エージェントの試行  ──────▶  キャッシュ：入力が同じなら再利用                なし（同じ入力の過去の有効な結果）
                    ──────▶  スモーク（1 試行）とフル（3 試行）を使い分け     スモークは傾向確認用と割り切る
                    ──────▶  試行・実行全体の上限                           上限到達は失敗 or 未実行として明示
 LLM 判定           ──────▶  コード判定が全合格のときだけ呼ぶ※             なし（不合格は判定しても合否が変わらない）
                    ──────▶  haiku・ツールなし・1 ターン・最大 2 万文字      較正（calibrate）で一致率を確認
                              （Claude Code で判定する場合。--judge-agent codex は読み取り専用だがツール・ターンの制限はなく、費用も不明）
 再試行             ──────▶  環境起因（レート制限・過負荷）だけ               なし（不合格を引き直さない）
```
※ 配布物がないと通らない判定（スキル・サブエージェント・hook を使うことの要求）の不合格だけなら、LLM 判定を省きません（基準との比較用スコアに必要なため）。

キャッシュキーに入るもの（どれか 1 つでも変われば再実行）:

```
 タスク定義 ＋ フィクスチャの中身 ＋ 採点スクリプトの中身
 ＋ プロファイル定義 ＋ 配布物（copy 元・hash_paths）の中身
 ＋ CLI のバージョン ＋ 試行番号
 ＋ 経路の変数（名前。秘密でないものは値のハッシュも。キー・トークン・ヘッダの値は使わない）
 ＋ （llm グレーダーがあるタスクだけ）判定エージェント・モデル・借りるプロファイルの環境変数・実行ファイル
       │
       └─▶ SHA-256 ─▶ .harness-eval-cache/<キー>.json
```

| 仕組み | 内容 |
|---|---|
| キャッシュ | 上の図の材料がすべて同じ試行は、過去の有効な結果を再利用（`.harness-eval-cache/`）。**変えた配布物のプロファイルだけ再実行**される。タスクの説明・タグ・`paired_negative`、プロファイルの説明・`billing` は含めない（書き換えても再実行しない）。グレーダーや採点スクリプトを直すと、そのタスクは全プロファイルで再実行される。`--no-cache` で無効 |
| LLM 判定の省略 | コード判定がすべて合格した試行にだけ LLM 判定を実行（配布物がないと通らない判定の不合格は除く。不合格の試行は判定しても合否が変わらないため）。判定は既定で Claude Code の haiku・ツールなし・1 ターン・対象は最大 2 万文字（`--judge-agent codex` は読み取り専用だがツール・ターンの制限はなく費用も不明） |
| 2 段構成 | 普段は `--tag smoke --no-llm`（1 試行）、リリース前・モデル移行時だけフル（3 試行 + LLM 判定） |
| 上限 | 試行ごと（`max_turns`・`max_budget_usd`・`timeout_s`）、実行全体（`--max-cost-usd`。推定額の合計で、サブスクリプションも含む）、月（`--monthly-budget-usd`: Claude と Codex を別々の口座として、費用台帳 `--out/spend.jsonl` の今月分を口座ごとに合計し、残りを上限にする。LLM 判定は判定エージェントの口座。`billing: subscription` は数えない。台帳には再試行・中断・`doctor`・`harvest` を含め費用が発生するたびに追記。`--out` を変えると別の台帳になる。`plan` で口座ごとの残りを確認できる）。到達後の試行は未実行として除外。費用が分からない試行（pricing なしの Codex、Codex での判定）は上限の計算に入らず、`--jobs` が 2 以上だと実行中の分だけ超えることがあります（実行前に注意を表示） |
| 見積もり | `plan` で実行前に呼び出し回数・キャッシュ・過去実績からの概算費用を確認 |
| 再試行 | レート制限・過負荷で落ちた試行だけ再試行（`--retry-infra` 既定 1 回、`--retry-wait-s` 既定 20 秒）。不合格の試行を「良い結果が出るまで」再実行はしない |

**キャッシュに入れないもの**: 除外・未実行・中断した試行と、タイムアウトした試行（一時的な原因でも起きるため）。
`--no-cache` のときは読みも書きもしません（`doctor` も書きません）。

**キャッシュの注意**: 環境変数による別名の付け替え（`ANTHROPIC_DEFAULT_SONNET_MODEL` など）はキャッシュキーに入りますが、サービス側でモデルの別名（`sonnet` など）が指す実体が変わっても、キーは変わらず結果は変わりえます。
モデル移行の評価やリリース判定では `--no-cache` を付けるか、モデル ID を固定してください。

**やらないこと**: 早期打ち切り（理由は [`docs/design.html`](docs/design.html) の「採らなかった案」）。

## 8. 出力

```
results/
  spend.jsonl      費用台帳（1 行 {at, usd, run_id, agent, billing}。費用が発生するたびに追記。--monthly-budget-usd が口座ごとに使う）
results/<実行ID>/
  report.html      レポート
  summary.json     集計（実行メタデータ: CLI 版・プロファイル/タスクのハッシュ・判定モデル・オプションを含む）
  trials.jsonl     試行レコード（1 行 1 試行）
  trials/<タスク>/<プロファイル>/<n>/stdout.jsonl, stderr.txt, workdir/
```

stdout.jsonl はエージェントの生出力（プロンプトと応答を含むトランスクリプト）です。`--keep none` で生ログと作業ディレクトリは消えますが、
**trials.jsonl・キャッシュ（`.harness-eval-cache/`）・report.html には最終応答とツール呼び出しの入力（書き込んだ内容やコマンド）が残ります**。
評価には合成データだけを使い、結果フォルダとキャッシュを社外に出さないでください。一時ホーム（認証情報の複製を含みうる）は中断時も含めて毎回削除します。

**中断（Ctrl-C）**: 未開始の試行は取り消し、実行中のエージェントは子プロセスごと停止します（トークンを使い続けない）。その実行の集計とレポートは作られませんが、`trials.jsonl` に完了済みの試行が残り、完了した試行はキャッシュされているので、同じコマンドの再実行で続きから実行されます（中断で止めた試行は `interrupted` としてキャッシュせず、再実行でやり直します）。

## 9. リリースゲートと較正

```bash
# 前回リリース時の結果と比べたリリース判定（CI で使う）。マージンは結果を見る前に業務への影響から決め、毎回明示する
python -m harness_eval gate --current results/<今回> --previous results/<前回> --margin 0.05
```

| 判定 | 条件（前回との差の 95% 区間、マージン m） | 終了コード |
|---|---|---|
| 通過 | 区間の下端 ≥ −m（悪化していても m 以内と言える） | 0 |
| 不通過 | 区間の上端 < −m（はっきり悪化）、安全違反の失敗分類（既定 `unsafe_access`。`--hard-category` で追加でき、既定は常に含む）が 1 件でもある、または比較できない（k・LLM 判定の有無・タスク定義が違う、今回の結果にプロファイル・タスクがない、指標を算出できない） | 1 |
| 判定不能 | それ以外、または比較できたタスクが `--min-tasks`（既定 10）未満 | 3 |
| （判定できない） | 定義の誤り・結果のフォルダがない（`summary.json`・`trials.jsonl`） | 2 |

区間はタスク単位のブートストラップ（試行を独立な標本として数えない）。タスクが少ないと区間は実際より狭く出やすいため、判断には 20 タスク以上・
3 試行以上を目安にしてください。前回より下がったタスクの一覧は参考として表示します（判定には使いません）。指標は `--metric`（既定 `pass_hat_k`）。

```bash

# LLM 判定と人手ラベルの一致率（約 20 件で較正）
python -m harness_eval calibrate results/<実行ID> --labels labels.yaml
```

`labels.yaml` の形式: `- {task: csv-summary-insight, profile: claude-ds-lite, trial: 0, grader: 2, label: PASS}`
（`grader` はタスク内のグレーダーの番号、0 始まり）。

## 10. 制約と注意（確認済みの事実と未確認事項）

開発時に実機・公式ドキュメント・ソースで確認したこと:
- Claude Code 2.1.304: `-p` はプロンプトを標準入力から読む（複数行・引用符・日本語を含めて実機で確認）。本ツールはプロンプトを
  標準入力で渡すため、コマンドライン長の制限や引用符の扱いの影響を受けない。
- Claude Code 2.1.304: `-p` で `--output-format stream-json` には `--verbose` が必須。`CLAUDE_CONFIG_DIR` 配下の CLAUDE.md とスキルが読まれる。
  Skill ツールの入力は `{"skill": 名前, "args": …}`。init イベントに CLI 版・モデル・読み込まれたスキル/プラグイン/MCP が入る。
- Claude Code: 一時ホームにしても **claude.ai アカウントのスキル/プラグイン同期**で配布物以外が混入する（実測 94 件）。
  `--settings` の `syncClaudeAiSkills:false` / `syncClaudeAiPlugins:false` で 23 件まで減り、残りは Claude Code 同梱スキルと
  実行環境側で管理されたプラグインだった。**完全には隔離できない**（上位フォルダの指示も読まれる。§3.3 のとおり `run` が止める）ため、各試行で実際に読み込まれた構成要素を記録し、
  レポートの「実行環境の記録」と `doctor --live` で見えるようにしています。組織の managed settings も隔離できません。
- Codex 0.163.0: `codex exec --json` のイベントは `thread.started` / `turn.*` / `item.*` / `error` のフラットな形式
  （`codex-rs/exec/src/exec_events.rs` で確認。一部の記事にある「エンベロープ形式」はソースでは確認できなかった）。
  `error` イベントは再接続の通知で致命的ではない。ネットワーク不通時は終了せず再接続を待ち続けるため、タイムアウトが必須。
- Codex 0.163.0: OS の一時ディレクトリ配下の `CODEX_HOME` だと補助バイナリの作成を拒否する警告が出る。
  そのため試行ディレクトリは `--out`（既定 `results/`）配下に作ります。`--out` を一時ディレクトリにしないでください。
- Codex には `--max-turns` に当たるフラグがないため、Codex の試行は `timeout_s` だけで止まります。
- セッション履歴の形式（§4.1 の表）: Claude Code 2.1.308 は対話モードを疑似端末で実際に操作して確認（貼り付けを含む）。
  Codex はこの環境から API に接続できず履歴が書かれないため、0.163.0 のソース（`message-history`・`protocol`）で確認。
- Codex 0.163.0: ネットワーク不通時は `Reconnecting...` の `error` イベントだけが続き、作業は進まない。この形で終わった試行は `network`（除外）にします。
- Claude Code 2.1.306: `--include-hook-events` で hook ごとに `hook_started` / `hook_response` イベント（`hook_name`・`exit_code`・`outcome`）が出る。
  配布物の PreToolUse hook が `rm` をブロックした試行で `outcome: error`・`exit_code: 2` を確認。
- Codex 0.163.0: 一時ホームの hooks は信頼確認がないと実行されない（上記）。`--dangerously-bypass-hook-trust` 使用時は、
  その旨の通知が `error` アイテムとして 2 回出る（致命的ではなく、警告として記録するだけ）。

未確認・未実装:
- **Windows では未検証**です（テストは Linux と macOS、Python 3.10・3.11・3.12・3.13 で実施）。パスは pathlib、タイムアウト時の停止は
  `taskkill /T` を使う実装ですが、社内 Windows 端末で `doctor --live` とスモーク評価を一度通してから展開してください。
  CLI が npm の `claude.cmd` の場合、`--settings` の JSON などの引数が cmd.exe を経由します。問題が出たら `executable:` で実体（`.exe` や `node` + スクリプト）を指定してください。
- Codex での実際のエージェント実行（この開発環境から OpenAI API に接続できないため）。Codex のパーサは実出力（失敗時）と
  ソースのイベント定義から作ったテストデータで検証しています。社内で `doctor --live` を最初に実行してください。
- MCP のモックサーバーは同梱していません（プロファイルの install で設定ファイルを置き、`extra_args` で `--mcp-config` を渡せます）。
- Claude Code のサブエージェント定義を Codex 形式（`.codex/agents/*.toml`）に変換する機能はありません。

## 11. 開発

```bash
python -m unittest discover -s tests        # 偽の claude / codex CLI を使うためトークンは使わない
uv run pytest --cov                         # 開発用（pytest・カバレッジ 80% 以上・ruff は pyproject.toml の dev グループ）
mkdir -p dist && git archive --format=zip --prefix=agent-harness-eval/ -o dist/agent-harness-eval.zip HEAD -- . ':!tasks'   # 社内配布用の zip（作業メモの tasks/ は含めない）
```

`tests/fake_agents/` は実機で観測した出力形式を再現する偽 CLI、`tests/data/` は実機の出力（Claude Code の stream-json、
Codex の失敗時 JSONL、採点スクリプトの回帰用の実出力）です。

## 採らなかった案

理由とともに [`docs/design.html`](docs/design.html) の「採らなかった案」にまとめています（Promptfoo・BenchFlow などの外部エンジン、`claude plugin eval` だけで済ませる案、早期打ち切り、トークン節約のフラグなど）。
