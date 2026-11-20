# Agent Harness Eval

Claude Code と Codex の「ハーネス」（CLAUDE.md / AGENTS.md・Skills・Subagents・Hooks・MCP・モデル設定）を、
**同じタスクで、配布される形のまま実行して採点し、配布物なしの基準や前回の版と比べる**評価ツールです。

<img src="docs/img/overview.svg" alt="タスク・プロファイル・配布物・フィクスチャを入力に、harness-eval run が 1 試行ずつ隔離して実行・採点・集計し、report.html・summary.json・trials.jsonl を出力する" width="100%">

- 「タスク × プロファイル × 試行回数」を 1 コマンドで実行し、合格率・毎回合格する率（pass^k）・費用・失敗の種類を出す
- 差には 95% 区間と、良くなった・悪くなったタスクの数が付く。リリース判定は、事前に決めた許容幅で 通過／不通過／判定不能
- `run` の最後に、候補がベースラインより良くなったかを `IMPROVED`／`NO CHANGE`／`REGRESSED`／`INCONCLUSIVE` の 1 つで出す。
  妥当性のチェックは既定で有効（[評価の結論と判定役](#評価の結論と判定役既定の挙動)）
- train タスクの失敗から、改善役（読み取りだけの `claude -p`）が配布物の変更案を作る（`improve`）。評価用のタスク（holdout）は
  権限設定で読ませず、効くことを起動のたびに確かめる
- 配布物の変更案は、改善に使っていない評価用のタスク（holdout）で現行と比べて採否を出す（`promote`。自己改善ループの関門）
- 毎回読み込まれる量（開始時の文脈）と成功あたりの費用を測り、成功率を落とさず費用を下げた変更案も採用できる（`promote --objective efficiency`）
- 各試行は一時ホームで動き、普段の `~/.claude` / `~/.codex` には触らない。Linux では bubblewrap で試行の外（参照解・採点・他の試行・本物のホーム）を見せず、
  答えを見た形跡のある試行は無効、見えているテストを壊した試行は不合格にする（[ズル対策](#ズル対策既定で有効)）
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
`--site` は有効にした仮想環境（`.venv`）だけに書き込むので、必ず上で仮想環境を有効にしてから実行します。

```bash
python -m pip config --site set global.index-url "<社内 Artifactory の PyPI ミラー URL（管理者に確認）>"
python -m pip install -r requirements.txt
python -m harness_eval --version
```

一度だけ指定するなら `python -m pip install --index-url "<社内 Artifactory の PyPI ミラー URL（管理者に確認）>" -r requirements.txt`。
最後に `0.10.0` が表示され、終了コード 0 なら導入完了です。
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

`harness-eval 0.10.0 / Python … / PyYAML …`、各プロファイルの CLI 版と `経路:` が出て、終了コード 0 なら静的確認は成功です。
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

`組み合わせ 8 × 試行 1 = 8 試行`（4 タスク × 2 プロファイル）、`判定側（judge）: LLM 判定なし（費用 0）`、終了コード 0 が目印です。
初回の例は `解く側（solver）の概算: $0.0000（履歴がなく見積もれない呼び出し 8 回）`。
解く側の費用はキャッシュ内の過去の費用から概算し、キャッシュで省ける試行は除きます。
履歴がないときの $0 や見積もれない呼び出しは、**不明・不完全という意味で、無料ではありません**。

### ④ スモーク評価（1 試行）

**`run` はタスク入力を設定先のモデルに送り、料金が発生し得ます。`--no-llm` は LLM 判定だけを無効にします。**
合成データを使ってください。Linux では試行を bubblewrap の中で動かします（`bwrap` が要ります。[ズル対策](#ズル対策既定で有効)）。
macOS・Windows では隔離がなく、一時ホームは設定の隔離にとどまります（試行の外を見た形跡の検査だけ行います）。

```bash
python -m harness_eval run --tasks examples/tasks --profiles "examples/profiles/claude-*.yaml" --tag smoke --no-llm --baseline claude-baseline --out "$OUT"
```

終了コード 0 と `レポート:` の出力を確認します。レポートの「有効/全」・除外された試行・予算停止を確認してからスコアを読みます。
出力の先頭の `結論` は、このスモーク（4 タスク・1 試行）では `INCONCLUSIVE` になります。
比べたタスクが 5 件未満で、1 タスクあたりの試行も 2 回未満だからです。スモークは動作と傾向の確認用です。

### ⑤ フル評価（3 試行・LLM 判定あり）

リリース前・モデル移行時には次を使います。`--max-cost-usd 5` は**この run が追跡できる費用だけ**の上限で、
先の doctor・スモークの費用は含みません。判定の費用は含みます。実行前の見積もりが上限を超えると、何も実行せずに止まります（終了コード 2）。
プロバイダー側の厳密な支出制限ではなく、並列実行では超過し得ます。
`pricing` のない Codex など不明な費用は上限計算に入りません（[費用の制限](docs/guide.html#cost)）。

```bash
python -m harness_eval run --tasks examples/tasks --profiles "examples/profiles/claude-*.yaml" --trials 3 --baseline claude-baseline --max-cost-usd 5 --out "$OUT"
```

④ と同じく、終了コード 0・`レポート:` と、有効な試行の件数を確認します。
判定役の段階は既定の `--tier dev` です。LLM 判定が合否を決めた結論は「暫定: 採用判断には使えません」と出ます。
**採用・マージの判断に使うなら `--tier decisive` を付けます。**decisive は強いモデルで 3 回ずつ判定するので、判定の費用が大きく増えます。
同梱サンプルを Claude Code と Codex の両方の CLI がある環境で見積もると、判定側の上限は約 $16 でした（実測の履歴がないときの多めの仮定）。
`--max-cost-usd 5` のままでは始まらないので、先に `plan … --tier decisive` で見積もりを確かめてから上限を決めてください。
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

## 評価の結論と判定役（既定の挙動）

`run` は最後に、候補がベースラインより良くなったかを次の 1 つで出します。
候補は `--candidate` で指定します（既定はベースライン以外のすべてのプロファイル）。
結論は CLI の出力の先頭、`report.html` の冒頭の「結論」、`summary.json` の `verdicts` に出ます。

| 結論 | 意味 |
|---|---|
| `IMPROVED` | 差（候補 − ベースライン）の 95% 区間の下端が 0 より上 |
| `REGRESSED` | 区間の上端が 0 より下。または対照タスクではっきり後退した |
| `NO CHANGE` | 区間が 0 をまたぎ、±0.10（`min_effect`）の内側に収まる |
| `INCONCLUSIVE` | 区間が 0 をまたぎ ±0.10 より広い（検出力不足）。または下の妥当性チェックに掛かった |

結論には理由が付きます。あわせて、ベースラインの合格率、差と 95% 区間（タスクでクラスタした 2 段ブートストラップ）、
判定役の設定（段階・モデル・系統・盲検化・回数・校正の一致率）、妥当性チェックの結果を出します。
`run` の終了コードは結論によらず 0 です。結論で止めるのは `verdict`（下）です。

### 妥当性チェック（既定で有効）

次のどれかに掛かると `INCONCLUSIVE` です。結論より優先します。

- ベースラインの合格率が 20%〜80% の外（易しすぎる・難しすぎる。`floor`・`ceiling` で変更可）
- エラーで結果が出なかった試行（利用上限・レート制限・認証・`agent_error` など）が 20% を超える。
  旧版の結果が `agent_error` と記録した利用上限のメッセージも数えます
- 比べられたタスクが 5 件未満、または 1 タスク・1 条件あたりの有効な試行が 2 回未満（10 件未満は警告）
- 片側にしか値がないタスクがある（悪化が隠れうる）
- 対照タスク（`--control-tag`）の後退の疑い。はっきり後退していれば `REGRESSED`。
  指定したタグのタスクがない・対照タスクの有効な試行が足りないときも `INCONCLUSIVE`
- LLM 判定が合否を決めた試行があるのに、判定役が同系統だけ（別系統を確認できない）、または校正が足りない（6 件未満・一致率 80% 未満）。
  LLM 判定が合否を決めていなければ、このチェックは掛かりません

`--out` の上の指示ファイルなど、これまでの漏れの検査はそのままです。
答えを見た形跡のある試行（下の「ズル対策」）は、エラーと同じく無効な試行として数えます。

### ズル対策（既定で有効）

解く側のエージェントが、評価の答え（参照解・採点スクリプト・他の試行の結果・本物のホームの認証）を読んだり、
見えているテストを壊して合格したりしないようにします。

| 対策 | 既定 | 結果 |
| --- | --- | --- |
| 隔離（`sandbox: bwrap`） | Linux で有効 | 試行は bubblewrap の中で動く。見えるのは試行ディレクトリ（一時ホーム・作業ディレクトリ。書ける）、読み取り専用のシステム（`/usr` `/bin` `/lib*` `/etc` `/opt`）、CLI の本体、プロファイルの `sandbox_ro` だけ。`/home`・`/workspace`・`/tmp` は空。ネットワークはモデルの API のために残す |
| 答えを見た形跡（leak） | 常に | ツールの入力に試行の外の絶対パス（本物のホーム・`--out`・タスクのフォルダ・採点スクリプト・AHE 自身など）、評価の名前（`trials.jsonl` など）、3 段以上の `../`、上流リポジトリへのネットワーク参照があれば、その試行は無効（`valid=false`、`invalid_reason=leak`）。合格にも不合格にも数えない。参照解・採点に埋めたカナリア（`AHE-EVAL-CANARY-5d2e9b`、タスクの `canaries`）がトランスクリプトのどこかに出ても無効 |
| 見えているテストの改ざん（tamper） | 常に | 採点の前に、起動直前の作業ツリーと比べる。既存のテストファイルの削除・改名、確認の行が正味で減った、既存のテストへの skip、テストの設定で対象を狭める変更があれば不合格（`failure_tag: test_tamper`）。テストを直すこと自体が課題のタスクは `allow_test_changes: true` |
| 環境変数 | 常に | 親の環境から渡すのは許可リスト（`PATH`・`LANG`・`LC_*`・`TERM`・`TZ`・`SHELL`、文字コード（`PYTHONUTF8` など）、Windows の必須、プロキシと証明書）と `inherit_env` に書いた名前だけ。API キー・`GH_TOKEN`・親のセッションの変数は、宣言しなければ渡らない |
| 一時ファイル | 常に | 試行の `TMPDIR` と、`command` 採点の `TMPDIR` は試行ごとのフォルダで、終わったら消す（隠しテストを当てた木を OS の `/tmp` に残さない） |
| Web 検索 | 常に | Codex の Web 検索は、タスクの `allow_tools` に `WebSearch`・`WebFetch` があるときだけ有効（Claude と同じ条件） |

verdict の検査欄 `integrity` に、無効にした試行・改ざん・隔離なしの試行の数が出ます。
隔離のない試行で候補が答えを見に行った形跡があれば、トランスクリプトに出ない読み方もありうるため `INCONCLUSIVE` にします。

隔離を使えない環境（`bwrap` がない・コンテナでユーザー名前空間が使えない）では、`run` が試行を始める前に止まります。
`apt install bubblewrap` などで入れるか、隔離しない場合はプロファイルに `sandbox: off` と書きます（結果に記録され、verdict が警告します）。
層の hook が試行の外のスクリプトを呼ぶ場合は、そのパスを `sandbox_ro` に並べます（隔離の中に読み取り専用で見せ、leak 検査でも許可します）。

**認証ファイルの複製（`copy_credentials: true`）の注意**: 試行の中の CLI が複製したログインを更新すると、更新用のトークンは使い回せないため、
本物のログインが使えなくなります（Codex の `auth.json` で実際に起きました）。そこで次のようにしています。

- `run` は始める前に本物の認証の期限を確かめ、期限まで 2 時間ない・切れている、または Codex の最終更新が 7 日より前なら止まります。
  `codex login`（または一度 `codex` を実行して更新）してからやり直してください。`doctor` が期限を表示します（値は表示しません）
- それでも試行の中で更新されたら、本物がまだ複製したときのままのときだけ、ロックを取って原子的に書き戻します
- 会社の評価では、更新の起きない API キー（Codex は `CODEX_API_KEY`、Claude は `ANTHROPIC_API_KEY`）を `inherit_env` で渡すことを勧めます

**判定役の認証をその場で使う（`credentials: in_place`。Codex を ChatGPT のログインで使う場合）**: API キーがなく `~/.codex/auth.json` しかないときの、
判定役専用の経路です。認証ファイルを複製しないので、複製側の更新で本物の refresh token が使用済みになる事故が起きません。
`--judge-profile codex=examples/judge-profiles/codex-judge-inplace.yaml` のように、実行には使わないプロファイルを渡します。

- 解く側のプロファイル（`--profiles`）に書くと `SpecError` で止まります（解く側に本物の設定ディレクトリを見せないため）。`claude_code`・`copy_credentials: true` との併用も `SpecError` です
- `HOME` は一時ホームのままで、`CODEX_HOME` だけ本物（環境変数 `CODEX_HOME`、なければ `~/.codex`）を指します。
  `codex exec --json --ephemeral -s read-only --skip-git-repo-check --ignore-user-config --ignore-rules --disable hooks` で呼び、
  利用者の `config.toml`（MCP・モデル・指示）・execpolicy の `.rules`・hooks を判定に入れません
- 同じ系統の in_place の判定は同時に 1 本だけです（`--jobs` が 2 以上でも順に呼びます。更新の競合を避けるため）。
  このロックは 1 つの `run` の中だけです。別の端末で同じ `~/.codex` を使う `codex` や別の `run` を同時に動かさないでください
- `run` は始める前に `auth.json` の存在と期限を確かめます（値は表示しません）。期限まで 2 時間ない・切れている、または最終更新が 7 日より前なら止まります。
  `codex login`（または一度 `codex` を実行して更新）してからやり直してください
- **限界**: `~/.codex/AGENTS.md` など `CODEX_HOME` に置いた指示ファイルが判定の入力に入るかは、`--ignore-user-config` の説明（`config.toml` を読まない）からは分かりません。
  この版（0.171.0）では実機で確かめていないので、入りうるものとして扱ってください。確かめるには、外側のシェルで 1 回だけ
  `HOME=$(mktemp -d) codex exec --json --ephemeral -s read-only --skip-git-repo-check --ignore-user-config --ignore-rules --disable hooks -C "$(mktemp -d)" "渡されている指示文の見出しだけを列挙して。なければ none"`
  を呼び、`AGENTS.md` の内容が出るかを見ます。出るなら、その指示が判定に混ざったものとして d の結果を読むか、実測の間だけ `AGENTS.md` を退避するか、API キー（`CODEX_API_KEY` を `inherit_env`）の経路に切り替えてください（認証ファイルを別のフォルダへ複製する方法は、事故の原因なので使いません）
- Claude は in_place にしません。本物の `~/.claude` を見せると `CLAUDE.md`・設定・フック・プラグインが判定に入るためです。
  サブスクリプションなら `claude setup-token` で作ったトークンを `CLAUDE_CODE_OAUTH_TOKEN` に入れ、`inherit_env` で渡します（`examples/judge-profiles/claude-judge-oauth.yaml`）

`--judge-profile` には、実行のプロファイルの id のほか、実行に使わない YAML のパスも渡せます（判定役の認証だけ借ります。配布物は使いません）。
系統ごとに `--judge-profile codex=path.yaml --judge-profile claude_code=path.yaml` と指定できます（系統が合わない YAML は `SpecError`）。

### 判定役（LLM 判定）

- 決定的な検査（ファイル・正規表現・スクリプトなど）に合格した出力にだけ走ります。見るのはルーブリック（`type: llm`）の基準だけです
- 渡すのは評価観点と、最終応答かファイル 1 つ（`max_chars` まで）だけです。会話の記録は渡しません
- 盲検化します。プロファイル id・モデル名・ハーネスの名前（CLAUDE.md・`.claude`・Codex など）・パスを伏せます。判定の順番は `--seed` で入れ替えます
- 既定は評価対象と別系統です。Claude Code の出力は Codex が、Codex の出力は Claude が判定します。
  別系統の CLI がなければ同系統で動きますが、「同系統の判定役」と警告し、LLM 判定が決めた結論は `INCONCLUSIVE` になります
- 判定の結果は `<--cache-dir>/judge/` に残します。キーは判定入力・モデル・ルーブリック・段階・何票目かのハッシュです。
  試行のキャッシュは判定の設定によらないので、`--tier` を変えても解く側は再実行しません

| 段階 | モデル | 回数 | 使いどころ |
|---|---|---|---|
| `--tier dev`（既定） | sonnet / gpt-6.2-sol | 1 系統 1 回 | 普段の確認。LLM 判定が決めた結論は「暫定: 採用判断には使えません」 |
| `--tier decisive` | opus / gpt-6.2-astra | 1 系統 3 回の多数決。両方の CLI があれば両系統の一致を求める（不一致は `judge_disagreement` で、合格にしない） | 採用・マージの判断 |

LLM 判定を使わない実行（決定的な検査だけ）は、どちらの段階でも採用判断に使えます。
haiku など、段階が求めるより弱いモデルは止めます。`--allow-weak-judge` を付ければ使えますが、
レポートに「弱い判定役」と出て、その判定が合否を決めた結論は採用判断に使えません。

**既定を dev にした理由**: 社内の評価は API の従量課金です。普段の判定は中位モデルで 1 回にし、
強いモデル・複数回・別系統の判定は、マージなど大事な判断のときだけ使う、という費用の方針に合わせています。
それでも、既定のまま弱い・安い判定で採用が決まることはありません。
LLM 判定が合否を決めた試行があれば、dev の結論は暫定です。関門の `verdict` は、そのとき decisive を求めます（dev なら終了コード 5）。
実行前の見積もりには、dev と decisive の両方の費用が出ます。
解く側の呼び出しに履歴（キャッシュの実測）がないときは、$0 とは数えず、タスクの `limits.max_budget_usd`（なければ $0.50）で見積もります。
`--max-cost-usd` は、この見積もりの合計が上限を超えると、何も実行せずに止まります。

### 費用

- 実行前（と `plan`）に、解く側（solver）と判定側（judge）を分けて、dev と decisive の両方の見積もりを出します。
  判定側は、全出力が決定的な検査に合格した場合の上限です。1 回あたりは判定キャッシュの実測、なければ多めの仮定を使います
- `--max-cost-usd` は判定の費用も含みます。見積もりが上限を超えると、何も実行せずに止まります（終了コード 2）。
  実行中に上限に達したら判定もやめます（その試行は `budget_stop` として集計から除く）
- 実行後は、solver・judge 別のトークン（入力・キャッシュ読み・キャッシュ書き・出力）・呼び出し回数・費用・判定の割合・
  判定キャッシュの再利用回数を出します（`summary.json` の `usage`）。費用は `harness_eval/prices.json`
  （公開 API 価格。出典 URL と取得日 2026-11-19 つき）で換算します。表にないモデルは CLI が返す費用を使い、それもなければ推測せず「不明」にします

### 事前登録と verdict

`run` は試行を始める前に、判定規則・そのハッシュ・判定役の設定を `prereg.json` に書きます。
規則は `--rules FILE.yaml` で変えられます（キーは [リファレンス「判定規則」](docs/reference.html#rules)）。

```bash
python -m harness_eval verdict "$OUT/<実行ID>"
```

`verdict` はモデルを呼ばずに既存の結果を判定し直し、`verdict.json` に書きます。旧版の結果も読めます。
規則を変えて集計し直すと「事後」と表示し、採用判断には使えません。`prereg.json` のハッシュが合わなければ `INCONCLUSIVE` です。

| 終了コード | 意味 |
|---|---|
| 0 | `IMPROVED`（採用してよい） |
| 1 | `REGRESSED` |
| 3 | `INCONCLUSIVE` |
| 4 | `NO CHANGE` |
| 5 | `IMPROVED` だが暫定（dev 段階・弱い判定役・事後の規則など。採用不可） |
| 2 | 定義エラー |

### 他のリポジトリの PR を関門にかける

1. プロファイルを 2 つ作ります。`before` はデフォルトブランチのハーネス、`after` は PR のブランチのハーネスです。
   2 つのブランチを別のフォルダに取り出し、それぞれの `install` の `copy` でそのフォルダを指します。`id` は `before`・`after` にします。
   タスクは 5 件以上（推奨 10 件以上）要ります。LLM 判定を使うなら、`calibration` を書いたタスクも入れます（校正に 6 件要るため）
2. 同じ run で両方を実行します。対照タスクがあれば `--control-tag control` も付けます

   ```bash
   python -m harness_eval run --tasks <タスク> --profiles before.yaml after.yaml --trials 3 --baseline before --candidate after --tier decisive --out "$OUT"
   ```

3. CI で `python -m harness_eval verdict "$OUT/<実行ID>"` を実行し、終了コード 0（採用してよい `IMPROVED`）のときだけマージを許します。
   `NO CHANGE`・`REGRESSED`・`INCONCLUSIVE`・暫定（終了コード 5）はマージしません

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

`tests/test_cheatproof.py` の隔離の確認は、`bwrap` が使える Linux だけで走ります（ないとスキップ）。
`tests/fake_agents/` は実機で観測した出力形式を再現する偽 CLI（`fake_cheater.py` はズルをする模擬エージェント）、`tests/data/` は実機の出力を元にしたフィクスチャです
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
