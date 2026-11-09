# Agent Harness Eval

Claude Code と Codex の「ハーネス」（CLAUDE.md / AGENTS.md・Skills・Subagents・Hooks・MCP・モデル設定）を、
**同じタスクで、配布される形のまま実行して採点し、配布物なしの基準や前回の版と比べる**評価ツールです。

<img src="docs/img/overview.svg" alt="タスク・プロファイル・配布物・フィクスチャを入力に、harness-eval run が 1 試行ずつ隔離して実行・採点・集計し、report.html・summary.json・trials.jsonl を出力する" width="100%">

- 「タスク × プロファイル × 試行回数」を 1 コマンドで実行し、合格率・毎回合格する率（pass^k）・費用・失敗の種類を出す
- 差には 95% 区間と、良くなった・悪くなったタスクの数が付く。リリース判定は、事前に決めた許容幅で 通過／不通過／判定不能
- train タスクの失敗から、改善役（読み取りだけの `claude -p`）が配布物の変更案を作る（`improve`）。評価用のタスク（holdout）は
  権限設定で読ませず、効くことを起動のたびに確かめる
- 配布物の変更案は、改善に使っていない評価用のタスク（holdout）で現行と比べて採否を出す（`promote`。自己改善ループの関門）
- 毎回読み込まれる量（開始時の文脈）と成功あたりの費用を測り、成功率を落とさず費用を下げた変更案も採用できる（`promote --objective efficiency`）
- 各試行は一時ホームで隔離し、普段の `~/.claude` / `~/.codex` には触らない
- 採点はコード判定が基本（独自スクリプトも可）。LLM 判定は補助
- 依存は Python 3.10+ と PyYAML だけ。各 CLI（`claude -p` / `codex exec --json`）を直接呼ぶ

用語の意味は末尾の [用語](#用語)、全体の関係は [図解ガイド「全体像」](docs/guide.html#overview) を参照してください。

## ドキュメント

| 知りたいこと | 文書 |
|---|---|
| 仕組み（図解）: 使う順番・実行の流れ・隔離・採点・指標・費用・harvest・出力・自己改善ループ（`improve`・`promote`） | 図解ガイド — [Web](https://r1ck29.github.io/agent-harness-eval/guide.html) / [`docs/guide.html`](docs/guide.html) |
| タスク・プロファイルの書き方、グレーダー、認証、コマンドのオプション | リファレンス — [Web](https://r1ck29.github.io/agent-harness-eval/reference.html) / [`docs/reference.html`](docs/reference.html) |
| なぜこの作りか、既存ツールとの比較、確かめた CLI の挙動、残るリスク | 設計記録 — [Web](https://r1ck29.github.io/agent-harness-eval/design.html) / [`docs/design.html`](docs/design.html) |

どれも 1 ファイルの HTML で、外部と通信しません。clone や zip で手元にあるファイルをブラウザで開いても読めます。

## 導入

### ① Python と依存パッケージ

Python **3.10 以上**を用意します。リポジトリは `git clone https://github.com/R1ck29/agent-harness-eval.git`
または **Code → Download ZIP** で取得します。
Git は clone する場合だけ必要です。展開したフォルダに移動してから、仮想環境を作ります。

macOS / Linux（Bash）:

```bash
cd agent-harness-eval  # ZIP の場合は展開したフォルダ名
python3 --version     # 3.10 以上であること
python3 -m venv .venv
source .venv/bin/activate
```

Windows（PowerShell）:

```powershell
cd agent-harness-eval  # ZIP の場合は展開したフォルダ名
python --version      # 3.10 以上であること
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**インストール前に**、管理者に `<社内 Artifactory・認証の設定手順（管理者に確認）>` を確認し、
PyPI ミラーと認証を設定してください。下はプレースホルダーです。実際の URL に置き換えます（両シェル共通）。

```bash
python -m pip config --site set global.index-url "<社内 Artifactory の PyPI ミラー URL（管理者に確認）>"
python -m pip install -r requirements.txt
python -m harness_eval --version
```

一度だけ指定するなら `python -m pip install --index-url "<社内 Artifactory の PyPI ミラー URL（管理者に確認）>" -r requirements.txt`。
最後に `0.8.0` が表示され、終了コード 0 なら導入完了です。
`python -m pip install .` で `harness-eval` コマンドとしても使えます（`python -m harness_eval` と同じ）。

### ② エージェント CLI

この手順には [Claude Code](https://code.claude.com/docs/en/setup) が必要です。
[Codex](https://developers.openai.com/codex/cli/) は Codex も評価する場合だけ用意します。
起動フラグと出力形式の**確認済み版（最低版ではありません）**は Claude Code **2.1.304** / Codex **0.163.0** です。
`claude --version` / `codex --version` で確認します。PATH にない場合はプロファイルの `executable:` で指定します。

### ③ 実行前の認証

`<社内アカウント・認証情報・モデル利用権限の申請ページ（管理者に確認）>` で、利用先と権限を準備してください。
試行は一時ホームで動くため、普段の macOS キーチェーンのログインは届きません。
以下のいずれかを選び、**実行する同じ端末**で設定します（例の値は置き換える）。

| 方式 | Bash | PowerShell | 両方の `examples/profiles/claude-*.yaml` に追記 |
|---|---|---|---|
| API キー | `export ANTHROPIC_API_KEY="<API キー>"` | `$env:ANTHROPIC_API_KEY="<API キー>"` | `inherit_env: [ANTHROPIC_API_KEY]`（`billing` は既定の `api`） |
| Bedrock | `export CLAUDE_CODE_USE_BEDROCK=1` / `export AWS_REGION=ap-northeast-1` | `$env:CLAUDE_CODE_USE_BEDROCK="1"` / `$env:AWS_REGION="ap-northeast-1"` | `inherit_env: [CLAUDE_CODE_USE_BEDROCK, AWS_REGION]` |
| サブスクリプション | `claude setup-token` → `export CLAUDE_CODE_OAUTH_TOKEN="<トークン>"` | `claude setup-token` → `$env:CLAUDE_CODE_OAUTH_TOKEN="<トークン>"` | `inherit_env: [CLAUDE_CODE_OAUTH_TOKEN]` と `billing: subscription` |

Bedrock は先に `<社内 AWS 認証・Bedrock モデル利用設定ページ（管理者に確認）>` に従い、AWS 認証とモデル利用権限を準備します。
表の `/` はコマンドの区切りです。秘密の値は YAML に書かず、変数名だけ `inherit_env` に書きます。
固定の Bedrock 設定を `env:` に書く例は `claude-baseline.yaml` のコメント、経路を変える他の変数の宣言は
[認証リファレンス](docs/reference.html#auth) を参照してください。

## 5 分で試す（同梱サンプル）

### 出力先

**書き込み可能で、リポジトリ・ホームの外**のフォルダを用意します。Linux の `/srv/harness-eval` は、
必要なら管理者に作成と書き込み権限の設定を依頼してください。
`--out` とその上に指示ファイルや設定フォルダがあると停止します。OS の一時フォルダは Codex が拒否するため避けます
（[隔離と制限](docs/guide.html#isolation)）。

```bash
OUT=/Users/Shared/harness-eval  # macOS
# Linux では上の代わりに:
# OUT=/srv/harness-eval
mkdir -p "$OUT"
```

```powershell
$OUT="C:\harness-eval"
New-Item -ItemType Directory -Force $OUT
```

以下のコマンドは両シェルで同じです（ワイルドカードは引用符ごと使います）。
ハーネスの設定・タスク・採点スクリプトは利用者の権限でローカル実行されるため、信頼できるものだけを評価してください。

### ① 定義の検証（モデル呼び出しなし）

```bash
python -m harness_eval validate --tasks examples/tasks --profiles examples/profiles --selftest
```

`OK: タスク 6 件、プロファイル 4 件` と、正解例のある 4 タスクの `selftest <タスクID>: OK`、終了コード 0 が目印です。
同梱サンプルでは Codex のスキル検出が推定であるという `警告:` が 2 件出ます。警告と selftest の失敗は別です。

### ② 環境の確認

```bash
python -m harness_eval doctor --profiles "examples/profiles/claude-*.yaml" --out "$OUT"
```

`harness-eval 0.8.0 / Python … / PyYAML …`、各プロファイルの CLI 版と `経路:` が出て、終了コード 0 なら静的確認は成功です。
`注意:` は内容を確認し、`NG:` や CLI の `見つかりません` は解消してから進みます。
静的確認だけでは認証の成功までは分かりません。

**次の `--live` は設定先のモデルを呼び、料金が発生し得ます**（各プロファイル 1 回）。

```bash
python -m harness_eval doctor --profiles "examples/profiles/claude-*.yaml" --out "$OUT" --live
```

両プロファイルで `最小試行: OK`、終了コード 0 が目印です。`NG` は解消してから進みます。

### ③ スモーク評価と同じ条件で見積もる（モデル呼び出しなし）

```bash
python -m harness_eval plan --tasks examples/tasks --profiles "examples/profiles/claude-*.yaml" --tag smoke --no-llm --baseline claude-baseline --out "$OUT"
```

`組み合わせ 8 × 試行 1 = 8 試行`（4 タスク × 2 プロファイル）、`LLM 判定: なし`、終了コード 0 が目印です。
初回の例は `概算費用: $0.0000（履歴がなく見積もれない呼び出し 8 回）`。
費用はキャッシュ内の過去の費用から概算し、キャッシュで省ける試行は除きます。
履歴がないときの $0 や見積もれない呼び出しは、**不明・不完全という意味で、無料ではありません**。

### ④ スモーク評価（1 試行）

**`run` はタスク入力を設定先のモデルに送り、料金が発生し得ます。`--no-llm` は LLM 判定だけを無効にします。**
合成データを使ってください。一時ホームは設定の隔離であり、外のファイルへのアクセスを禁止する sandbox ではありません。

```bash
python -m harness_eval run --tasks examples/tasks --profiles "examples/profiles/claude-*.yaml" --tag smoke --no-llm --baseline claude-baseline --out "$OUT"
```

終了コード 0 と `レポート:` の出力を確認します。レポートの「有効/全」・除外された試行・予算停止を確認してからスコアを読みます。

### ⑤ フル評価（3 試行・LLM 判定あり）

リリース前・モデル移行時には次を使います。`--max-cost-usd 5` は**この run が追跡できる費用だけ**の上限で、
先の doctor・スモークの費用は含みません。プロバイダー側の厳密な支出制限ではなく、並列実行では超過し得ます。
`pricing` のない Codex など不明な費用は上限計算に入りません（[費用の制限](docs/guide.html#cost)）。

```bash
python -m harness_eval run --tasks examples/tasks --profiles "examples/profiles/claude-*.yaml" --trials 3 --baseline claude-baseline --max-cost-usd 5 --out "$OUT"
```

④ と同じく、終了コード 0・`レポート:` と、有効な試行の件数を確認します。
`improve` もモデルを呼び、料金が発生し得ます（[自己改善ループ](docs/guide.html#loop)）。

### ⑥ 実行IDを使ってリリース判定

各 run が出す `レポート: …/<実行ID>/report.html` の親フォルダ名が実行IDです。
`ls "$OUT"`（Bash）/ `Get-ChildItem $OUT`（PowerShell）でも探せます。
配布物を変えたら ⑤ と同じオプションでもう一度 run し、変更前後の ID を指定します。

```bash
python -m harness_eval gate --current "$OUT/<変更後の実行ID>" --previous "$OUT/<変更前の実行ID>" --margin 0.05
```

同梱サンプルは 6 タスクで、判定に必要な 10 タスクに届かないため「判定不能」（終了コード 3）になります。
通過は 0、不通過は 1 です（[指標とリリース判定](docs/guide.html#metrics)）。

### 自分の履歴からタスクを作る

**`harvest` は、秘匿化・架空化する前の元のセッション履歴の依頼文を Claude（設定したプロバイダー）に送り、料金が発生し得ます。**
その送信先に送ってよい履歴だけを使ってください。まず `--dry-run` で件数だけ確認します
（[harvest の仕組み](docs/guide.html#harvest)）。

```bash
python -m harness_eval harvest --out my-tasks --limit 10 --profile examples/profiles/claude-baseline.yaml --ledger "$OUT/spend.jsonl" --dry-run
python -m harness_eval harvest --out my-tasks --limit 10 --profile examples/profiles/claude-baseline.yaml --ledger "$OUT/spend.jsonl"
```

dry-run は `（--dry-run: 書き換え・書き出しはしていません。依頼の本文は表示しません）` と終了コード 0 を確認します。
生成した下書き（`my-tasks/tasks/`）と **`checks/` のスクリプトを先にレビュー**してください。
`validate --selftest` でスクリプトが利用者の権限でローカル実行されます。
確認・修正後に `validate --tasks my-tasks/tasks --profiles … --selftest` → `run --tasks my-tasks/tasks …` で使います。

**同梱サンプル**（履歴がないときに `harvest` が書き出す標準タスクも兼ねる）: 合成の売上 CSV（`examples/fixtures`）を使う 6 タスクと、
小さな配布物 `examples/harnesses/ds-lite`（CLAUDE.md の出力形式ルール・「曖昧なら確認」ルール＋月別集計スキル）。
4 タスクには正解例（`examples/references/`）があります。

| タスク | 想定している実際の依頼 | 主に見ること |
|---|---|---|
| `request-extract` | 依頼文どおりの条件で SQL 抽出し、結果と SQL を納品 | 条件の取り違えで「動くが違うデータ」を返さないか |
| `request-ambiguous` | 「先月の売上データを抽出して」だけの曖昧な依頼 | 推測で作業せず、確認の質問を返すか |
| `csv-summary-format` | 月別集計のレポート | 配布物の形式ルール（常時指示）が効くか |
| `csv-summary-skill` / `arithmetic-no-skill` | 集計レポート／単純な質問 | スキルが必要なときだけ呼ばれるか |
| `csv-summary-insight` | 集計＋考察 | LLM 判定（フル評価用） |

## 開発

[uv の導入手順](https://docs.astral.sh/uv/getting-started/installation/) に従って uv を用意します。
依存の取得先は社内ミラーに設定してください（uv のインデックス設定は pip とは別です）。

```bash
uv sync --group dev
uv run pytest --cov                         # カバレッジ 80% 以上
uv run ruff check .
python -m unittest discover -s tests        # 偽 CLI を使い、トークンは使わない
```

実機の読み取り制限も確かめる場合（費用 0。指定しないと 1 件スキップ）:

```bash
HARNESS_EVAL_REAL_CLI=1 python -m unittest discover -s tests
```

```powershell
$env:HARNESS_EVAL_REAL_CLI="1"
python -m unittest discover -s tests
Remove-Item Env:HARNESS_EVAL_REAL_CLI
```

`tests/fake_agents/` は実機で観測した出力形式を再現する偽 CLI、`tests/data/` は実機の出力を元にしたフィクスチャです
（Claude Code の stream-json は識別子・パス・拡張一覧を匿名化）。

## 用語

| 用語 | 意味 |
|---|---|
| プロファイル | エージェント・モデル・展開する設定をまとめた YAML |
| ハーネス / 配布物 | 評価する設定一式。一時ホームに展開する |
| Skills | 必要に応じて読み込む手順（`SKILL.md`） |
| Subagents | 仕事の一部を任せるサブエージェント |
| Hooks | CLI のイベントに応じて実行する処理 |
| MCP | 外部ツールやデータにつなぐ仕組み |
| holdout | 改善には使わず、変更案の評価に使うタスク |
| pass^k | k 回続けて合格する確率 |
| 実行ID | 1 回の run の結果フォルダ名 |

全体の関係は [図解ガイド「全体像」](docs/guide.html#overview) を参照してください。

## ライセンス

MIT（[`LICENSE`](LICENSE)）。
