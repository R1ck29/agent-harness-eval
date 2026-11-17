# agent-harness-eval 再設計（設計書）

- 対象: agent-harness-eval 0.8.0 → 0.9.0（今回の中核の実装）→ 以降のフェーズ
- 作成: 2026-11-17。実機の確認は Claude Code 2.1.325・codex-cli 0.169.0（Linux）で行った
- 位置づけ: 仕組みの説明（`docs/design.html`）は「なぜ今の作りか」の記録。この文書は「次にどう作り替えるか」の設計で、
  後続のフェーズは別の担当（別のモデルを含む）がこの文書だけを見て実装できるように書く

## 0. 結論（先に要点）

1. **全面的な作り直しはしない。** 隔離（一時ホーム・経路の変数・祖先の指示の検出）、ペアの統計（タスク単位のブートストラップ・符号検定）、
   gate・promote・holdout 台帳は、実測とテストで確かめた資産で、OSS の評価ツール（§4）と比べても劣らない。
   作り替えるのは「**測っている対象が本当に候補のハーネスか**」を保証する部分と、OSS で標準になった指標の不足分だけにする。
2. **最重要の問い（試行は候補のハーネスを本当に読み込むか）への答え**は、置き場所によって違う（§2 で実測）。
   - ユーザー単位の置き場所（`{config_dir}`＝一時の `~/.claude`・`CODEX_HOME`、`~/.agents/skills`）は、**今の版でも読み込まれる**。
     同梱の例（`examples/profiles/*-ds-lite.yaml`）は、この場所に置くので正しく測れている。
   - **プロジェクト単位の置き場所（作業ディレクトリの `.claude/settings.json`・`.codex/config.toml`）は、黙って一部が捨てられる。**
     Claude Code は `permissions.allow` を、Codex は `.codex/config.toml` 全体（MCP・指示を含む）を、未信頼のフォルダでは使わない。
     しかも試行の結果には何も出ない（Claude Code は標準エラーに 1 行出すだけ、Codex は何も出さない）。
   - Claude Code で `CLAUDE_CONFIG_DIR` を設定すると、MCP の設定は `$CLAUDE_CONFIG_DIR/.claude.json` から読まれ、
     `{home}/.claude.json` に置いた MCP は**読まれない**。
   - Codex の利用上限（「You've hit your usage limit」）は、今の分類では**エージェントの失敗として数えられる**（環境起因として除外されない）。
3. 直し方（0.9.0 で実装）:
   - 隔離した設定の中で、作業ディレクトリを「信頼済み」にしてから起動する（`trust_workdir`、既定で有効）。普段の利用者は自分のリポジトリを
     一度信頼しているので、その状態を再現する。今の版が Codex の hooks に `--dangerously-bypass-hook-trust` を付けているのと同じ考え方。
   - **試行ごとの有効設定の記録**（`effective_config.json`、レコードの `effective`）: 置いたファイル（ハッシュ）、期待した部品（スキル・
     サブエージェント・MCP・常時指示）、CLI が実際に読み込んだ部品、その差（読み込まれなかったもの）、信頼の付与、起動コマンド。
     読み込まれなかった部品があれば集計とレポートに出す。
   - 利用上限を環境起因（`usage_limit`）として除外する。
   - 指標: **スキルの呼び出し率**（使うべきタスク）と**誤発火率**（使うべきでないタスク＝否定の対照）を Wilson の 95% 区間付きで、
     **pass@k**（少なくとも 1 回合格する確率の不偏推定）と **pass@1 の 95% 区間**を加える。スキルの効果（lift）は既存の Δ（比較用の値）をそのまま使う。
4. 版は **0.9.0（後方互換）**。CLI の引数・設定キー・ファイル形式・作業フォルダの構成は壊さない（追加だけ）。
   ただし `trust_workdir` の既定により、**プロジェクト単位にハーネスを置くプロファイルは測定値が変わる**（正しくなる）ため、
   アップグレード後は基準の結果を取り直してから gate する（CHANGELOG の「アップグレード方法」）。
5. 後続（§10）: アダプタを共有ライブラリ `agent_cli`（標準ライブラリだけ、複製して同梱）へ移す、SkillsBench／skilljack 形式のタスクの取り込み、
   gate の読み込み検査、doctor の費用 0 の読み込み確認、など。

## 1. 現在のアーキテクチャ（0.8.0）

```
tasks/*.yaml ─┐                         ┌─ adapters.py（CLI ごとのコマンド組み立て・出力の解析）
profiles/*.yaml┴─ spec.py（検証・ハッシュ）─ runner.py ─┼─ isolation.py（一時ホーム・環境変数・install・プロセス実行）
                                          │            ├─ graders.py（成果物・過程・LLM 判定）
                                          │            └─ record.py（正規化した試行レコード）
                                          └─ metrics.py（集計・Δ・gate・promote）→ report.py（HTML）
cli.py: validate / plan / run / report / gate / promote / doctor / harvest / improve / skill-variants / calibrate
```

| 部分 | 役割 | 主な場所 |
|---|---|---|
| タスク | YAML 1 ファイル。`prompt`・`fixture`・`limits`・`allow_tools`・`graders`・`paired_negative`・`split`・`family` | `harness_eval/spec.py:59`（`TASK_KEYS`） |
| プロファイル | エージェント・モデル・`install`（copy / run）・`env`・`inherit_env`・`billing` など | `harness_eval/spec.py:68`（`PROFILE_KEYS`） |
| 試行の隔離 | `<run>/trials/<task>/<profile>/<n>/{home,workdir}`。`HOME`・`USERPROFILE` を一時ホームに、`CLAUDE_CONFIG_DIR`／`CODEX_HOME` を `home/.claude`／`home/.codex` に向ける | `harness_eval/isolation.py:114`, `:138`, `:150` |
| 起動 | Claude Code: `claude -p --output-format stream-json --verbose …`（プロンプトは標準入力）。Codex: `codex exec --json --skip-git-repo-check --ephemeral -C <wd> -s <sandbox> --dangerously-bypass-hook-trust … -` | `harness_eval/adapters.py:92`, `:226` |
| 解析 | Claude Code の `system/init` から読み込まれたスキル・プラグイン・MCP・サブエージェントを `meta` に記録。Codex はシェルのコマンドから SKILL.md の読み込みを推定 | `harness_eval/adapters.py:121-129`, `:217` |
| 採点 | 試行スコア = 合格したグレーダーの重み付き割合。`harness_only`（配布物がないと通らない判定）を除いた比較用の値も出す | `harness_eval/graders.py:149` |
| 集計 | タスク単位の pass@1・pass^k・平均スコア・費用・トークン・開始時の文脈。Δ はタスク単位のブートストラップ 95% 区間と符号検定 | `harness_eval/metrics.py:184`, `:254` |
| 関門 | gate（前回との非劣性）・promote（holdout で候補 vs 現行、評価した候補の数で α を割る） | `harness_eval/metrics.py` 後半 |
| 予算 | 試行ごと（`max_turns`・`max_budget_usd`・`timeout_s`）、実行全体（`--max-cost-usd`）、口座ごとの月（`--monthly-budget-usd`、台帳 `spend.jsonl`） | `harness_eval/runner.py` |
| キャッシュ | タスク・プロファイル・配布物・CLI 版・試行番号・経路の変数・判定設定・ツール版が同じなら再利用 | `harness_eval/runner.py:39`, `cache_key` |

試行 1 回の流れ（`harness_eval/runner.py:375` `_execute_in`）: フィクスチャを作業ディレクトリへ複製 → 環境変数を組み立て → 認証ファイルの複製（任意）
→ `install` → 作業ディレクトリのスナップショット → 起動 → 出力の解析 → 異常終了の分類（環境起因なら無効）→ 採点 → 一時ホームの削除。

## 2. 互換性の検証: 試行は候補のハーネスを読み込むか（実測）

### 2.1 方法

費用 0 で確かめられる観測点を使った（モデルを呼ばない）。

- Claude Code: `claude -p --output-format stream-json --verbose` は、**ログインしていなくても** API を呼ぶ前に `system/init` を出す。
  ここに `skills`・`agents`・`mcp_servers`（名前・`source`）・`plugins` が並ぶ。未ログインの結果は `result`（`subtype: success`・`is_error: true`・
  「Not logged in」）で終わる。
- Codex: `codex debug prompt-input` が「モデルに渡す入力」を JSON で出す。`<skills_instructions>` にスキルの一覧
  （`- 名前: 説明 (file: rN/名前/SKILL.md)`）、`# AGENTS.md instructions for <cwd>` に常時指示の本文が入る。1 回 約 1.4 秒。
  `codex mcp list --json` が有効な MCP の一覧を出す。
- MCP が**実際に起動されたか**は、起動されると印のファイルを作るだけの MCP サーバーを置いて確かめた（Codex は利用上限で
  モデルの呼び出しが失敗しても、その前に MCP を起動する）。
- 条件: 一時ホーム（`HOME`）・`CLAUDE_CONFIG_DIR=$HOME/.claude`・`CODEX_HOME=$HOME/.codex`・作業ディレクトリは別フォルダ（本番の試行と同じ構成）。
  ホストのセッションの `CLAUDE*` 変数は外した。プローブのスクリプトは §11 に記録。

### 2.2 Claude Code 2.1.325（`claude -p`）

| 置いた場所 | 読み込み | 根拠 |
|---|---|---|
| `{config_dir}/skills/<名前>/SKILL.md`（`allowed-tools` あり・`hooks` ありを含む） | **読み込まれる** | init の `skills` に 3 つとも出た |
| `{config_dir}/agents/<名前>.md` | 読み込まれる | init の `agents` |
| `{config_dir}/.claude.json` の `mcpServers` | **読み込まれ、起動される**（`source: user`） | init と起動の印 |
| `{home}/.claude.json` の `mcpServers` | **読み込まれない** | `CLAUDE_CONFIG_DIR` を設定すると全体設定はその中の `.claude.json` になる。印も作られない |
| `{workdir}/.claude/skills`・`.claude/agents` | 読み込まれる（未信頼でも） | init（`proj-*`） |
| `{workdir}/.mcp.json` | 読み込まれ、起動される（未信頼でも、承認なしで） | init（`source: project`）と印。公式文書の表とも一致 |
| `{workdir}/.claude/settings.json` の `permissions.allow` | **未信頼では使われない** | 標準エラーに `Ignoring 1 permissions.allow entry from .claude/settings.json: this workspace has not been trusted … set projects["<path>"].hasTrustDialogAccepted: true in <CLAUDE_CONFIG_DIR>/.claude.json` |
| 同上（`$CLAUDE_CONFIG_DIR/.claude.json` に `projects[<workdir>].hasTrustDialogAccepted: true` を書いた場合） | **使われる**（警告が消える） | 実測 |
| 同上（作業ディレクトリが**親の git リポジトリの中**にあり、信頼を作業ディレクトリにだけ付けた場合） | **使われない** | 警告が「リポジトリのルート」を名指しする。信頼は git のルート単位（公式文書 permissions「Project allow rules and workspace trust」） |

公式文書（`permissions.md`「What runs before you trust a folder」）では、`claude -p` で未信頼のとき、ほかに
「プロジェクトのサブエージェントの frontmatter の hooks」「プロジェクトの `@skills-dir` プラグイン」「サブエージェントの frontmatter の `mcpServers`」も使われない。
信頼を付ければすべて使われる。

**問いの「allowed-tools や hooks を持つスキルは非対話・スケジュール実行で承認が必要」について:** 2.1.325 の変更は
「**scheduled tasks と `/loop`** で、`allowed-tools` または hooks を宣言したスキルを実行する前に確認する」（変更履歴 2.1.325）で、`claude -p` は対象外。
`-p` では「プロジェクトのスキルの `allowed-tools` は未信頼のフォルダでも適用される」（`skills.md`）。実測でも、これらのスキルは `-p` の init に出る。
したがって **`-p` で起動する試行は影響を受けない。** ただし、このツールの試行を scheduled task や `/loop` の中から起動しても、子の `claude -p` は別プロセスなので影響しない
（親のセッションの変数は `INHERITED_UNSET` で外している）。

**「サンドボックスの Bash は既定で確認する」について:** 2.1.325 から、サンドボックス内の Bash も通常の許可の流れに従う
（以前は自動で許可。`sandbox.autoAllowBashIfSandboxed: true` で元に戻る）。`-p` では確認は拒否と同じになる。
このツールはタスクの `allow_tools` を `--allowedTools` で渡すので、`Bash` を許可していれば影響しない。配布物の `settings.json` で
サンドボックスを有効にし、タスクで `Bash` を許可していない場合だけ、0.8.0 の頃と結果が変わる（Bash が拒否される）。これは
「利用者が同じ配布物を `-p` で使ったときの挙動」そのものなので、補正はせず、`validate` の警告で知らせる（§10 フェーズ 4）。

### 2.3 Codex 0.169.0（`codex exec`）

| 置いた場所 | 読み込み | 根拠 |
|---|---|---|
| `CODEX_HOME/AGENTS.md` | 読み込まれる | prompt-input に本文 |
| `~/.agents/skills/<名前>/SKILL.md`・`CODEX_HOME/skills/<名前>/SKILL.md` | 読み込まれる | prompt-input の `<skills_instructions>` |
| `CODEX_HOME/config.toml` の `[mcp_servers.*]` | 読み込まれ、**`exec` で起動される**（未信頼でも） | `mcp list` と起動の印 |
| `{workdir}/AGENTS.md`・`{workdir}/.agents/skills` | 読み込まれる（未信頼でも） | prompt-input |
| `{workdir}/.codex/config.toml`（`developer_instructions`・`[mcp_servers.*]`） | **未信頼では読み込まれない**（何も出力されない） | prompt-input に出ず、`mcp list` にもなく、起動の印もない |
| 同上（`CODEX_HOME/config.toml` に `[projects."<workdir>"] trust_level = "trusted"` を書いた場合） | **読み込まれ、起動される** | 3 つとも確認。作業ディレクトリが親の git リポジトリの中でも、作業ディレクトリへの信頼で読み込まれた |

`--trust`（「このプロジェクトを信頼済みとして保存する」）も同じ効果だが、旧版にはなく、`CODEX_HOME/config.toml` を実行中に書き換える。
一時の `config.toml` に起動前に書く方が、版に依存せず、何を付与したかを記録できる。0.163.0 で確かめた `--dangerously-bypass-hook-trust`・
`--ephemeral`・`--skip-git-repo-check`・`-C`・`-s`・`--json` は 0.169.0 にもある。`exec --json` のイベント
（`thread.started`・`turn.started`・`error`・`turn.failed`）も同じ形を確認した。

**利用上限:** ChatGPT のプランの上限に達すると `turn.failed` の `error.message` が
「You’ve hit your usage limit. … try again at Nov 19th, 2026 9:03 AM.」になる（実測）。`adapters.py:49` の `_classify_message` は
`rate.?limit|429|too many requests` だけを見るので `agent_error` になり、**有効な試行の失敗**として pass@1 を下げる（gate・promote の判断も歪む）。
この確認以降、モデルを呼ぶ確認はできなかった（`item.completed`・`turn.completed` の形は 0.163.0 の記録とテストデータに頼る）。

### 2.4 結論

| 問い | 答え |
|---|---|
| ユーザー単位に置いた配布物（同梱の例の形）は読み込まれるか | はい（両 CLI） |
| プロジェクト単位に置いた配布物は読み込まれるか | **一部が黙って捨てられる**（Claude Code: `permissions.allow` など。Codex: `.codex/config.toml` 全体） |
| `{home}/.claude.json` に置いた MCP は | **読み込まれない**（`{config_dir}/.claude.json` に置く） |
| 0.8.0 は読み込まれなかったことに気づけるか | Claude Code はスキル・MCP・プラグインの一覧を `meta` に残すが、**期待と照合しない**。Codex は何も残さない |
| 0.8.0 は利用上限を除外できるか | **できない**（失敗として数える） |

## 3. 課題（証拠付き）

| # | 課題 | 証拠 | 影響 |
|---|---|---|---|
| P1 | プロジェクト単位の配布物が未信頼で一部捨てられ、記録にも残らない | §2.2・§2.3 の実測 | 候補が「効かない」と誤判定される。gate・promote が誤った採否を出す |
| P2 | 読み込まれた部品を期待と照合しない | `adapters.py:121-129`（記録だけ）、`metrics.py:219`（一覧の和集合を出すだけ） | 配布物の置き場所の誤り・frontmatter の誤り・CLI の仕様変更で読み込まれなくても、「ハーネスの効果なし」に見える |
| P3 | Codex の読み込みを観測していない | `adapters.py:226`（起動だけ）。スキルは呼び出しの推定だけ（`adapters.py:217`） | 同上（Codex は特に気づけない） |
| P4 | 試行の有効設定（何を置き、何で起動し、何が読まれたか）が試行単位に残らない | `record.py`（`meta` にばらばら）、`summary.json` の `meta.profiles` はプロファイル単位 | 「codex が 97%」がどの設定の結果か後から確かめられない（agent-skill-eval の `run_meta.json` が解く問題） |
| P5 | 利用上限が失敗として数えられる | §2.3、`adapters.py:49` | 上限に当たった実行の pass@1 が下がり、gate が fail する |
| P6 | スキルの呼び出し率・誤発火率がない | `paired_negative` は `validate` の警告にしか使っていない（`spec.py` `lint`）。`metrics.py` に該当の指標なし | 「スキルが発火しない」という最も多い失敗（agent-skill-eval・skilljack の指摘）を数字で追えない |
| P7 | pass@k（少なくとも 1 回）と、プロファイル全体の pass@1 の区間がない | `metrics.py:161`（pass^k だけ）、区間は Δ にだけ付く | 試行回数の少ない実行で、絶対値の不確かさが分からない |
| P8 | アダプタが CLI の版ごとの差を吸収する層を持たない | `VERIFIED_VERSIONS` は警告だけ（`adapters.py:320`）。フラグは決め打ち | CLI の版上げ（両 CLI とも月に数回）に追従しにくい。wikiskill と同じ処理を二重に保守している |
| P9 | タスクの形式が独自で、OSS のタスク集（SkillsBench 形式）を使えない | `spec.py` の YAML のみ | 既存のタスク集を評価に流用できない |
| P10 | テストの CI がない | `.github/` がない | マージの前に壊れたことに気づけない |

## 4. OSS の評価ツールとの比較

| 観点 | このツール 0.8.0 | skilljack-evals | agent-skill-eval | coder_eval | skill-ab-eval |
|---|---|---|---|---|---|
| タスク形式 | YAML 1 ファイル + fixture フォルダ | `evals/<id>/task.md`（frontmatter + 本文のプロンプト）+ `environment/{workspace,skills}` + `verifier/`・`oracle/`（SkillsBench 形式） | `evals/evals.json`（ケースの配列）+ `files/` | 宣言的な YAML（依存を固定） | ケースごとの A/B セル |
| 実行 | 本物の CLI、試行ごとの一時ホーム | 本物の CLI（全権限）、作業フォルダは使い捨て。エージェントは隔離しない | 本物の CLI（native）または Harbor（Docker） | サンドボックス | セルごとに新しい文脈 |
| 基準との比較 | 基準プロファイルとの Δ（比較用の値、タスク単位のブートストラップ区間、符号検定） | Skill Lift（スキルあり − なし、タスクごと・マクロ平均） | with / without の差、比較用ベースライン JSON、ブートストラップ区間 | A/B の実験層 | lift と判定 |
| 発火の指標 | `skill_used` グレーダー（合否の一部） | **Skill Invocation Rate**（合否と切り離す）、**anti-trigger タスク**（`expect_skill_invocation: false`） | **trigger split**（陽性と**否定の対照**を分けて報告、不明は「発火せず」と数えない） | **`skill_triggered`** 基準 | — |
| 反復の指標 | pass@1・pass^k | Resolution Rate（95% 二項区間）・Pass@k | **pass@k と pass^k**（Wilson 区間） | 重み付きスコアと閾値 | — |
| 有効設定の記録 | `meta`（CLI 版・読み込んだ部品）と summary の profiles | キャッシュキー | **`run_meta.json`**（モデル・推論の強さ・接続先・CLI 版・ツール版） | テレメトリ | — |
| 予算 | 試行・実行・口座ごとの月 | — | **試行ごとのトークン・費用・時間の上限**（warn / fail / stop-suite） | — | — |
| CI の関門 | gate（非劣性マージン・最小タスク数・回帰予算）・promote（holdout・多重比較） | 解決率と lift の閾値（lift は fail closed） | ベースラインとの比較 | CI でビルドを止める | — |
| 信頼・承認の扱い | 0.8.0: Codex hooks だけ（`--dangerously-bypass-hook-trust`） | `--dangerously-skip-permissions` 等ですべて許可 | — | サンドボックス | — |

**取り入れるもの:** 発火の指標を合否から切り離す（skilljack・agent-skill-eval）、否定の対照を別に報告し「不明」を「発火せず」と数えない（agent-skill-eval）、
pass@k と Wilson 区間（agent-skill-eval）、試行ごとの有効設定の記録（agent-skill-eval の `run_meta.json` を試行単位に）、SkillsBench 形式のタスクの取り込み（skilljack）。

**取り入れないもの:** 「すべて許可」で起動すること（skilljack）は、利用者の普段の権限と違う条件で測ることになり、`allow_tools`・`deny_tools` の評価もできなくなる。
このツールは「普段の利用者が信頼済みのフォルダで使う状態」を再現し、権限はタスクで明示する方針を保つ。合否を LLM 判定と切り離す skilljack の方針は、
このツールでは LLM 判定をコード判定がすべて通ったときだけ使う形で既に近い。

**このツールが勝っている点（保つ）:** タスク単位のペアの区間（試行を独立に数えない）、比較用の値（`harness_only` を除いた Δ）、holdout と多重比較の補正、
経路の変数の検出（意図しない口座・モデルで実行しない）、祖先の指示の検出、口座ごとの月の予算。

## 5. 選択肢と判断

| 論点 | 選択肢 | 判断と理由 |
|---|---|---|
| 作り直しの範囲 | (a) 全面的に書き直す (b) 測定の正しさに関わる部分と不足する指標だけ作り替える | **(b)**。§4 のとおり統計と関門は OSS より厳密。書き直しは既存の利用者の結果・タスク・キャッシュを壊し、利点がない |
| プロジェクト単位の配布物の信頼 | (a) 何もしない（未信頼のまま測る） (b) 一時の設定に信頼を書く (c) Codex `--trust` / Claude Code に相当するフラグ | **(b)**。(a) は利用者の普段の状態（自分のリポジトリは信頼済み）と違う。(c) は Codex 0.169 にしかなく、Claude Code にはフラグがない。(b) は両 CLI で実測で確認でき、版に依存しない。プロファイルの `trust_workdir: false` で (a) も選べる（未信頼のリポジトリでの挙動を測りたい場合） |
| 信頼を付ける範囲（Claude Code） | 作業ディレクトリだけ／git のルートを探す／作業ディレクトリとすべての祖先 | **作業ディレクトリとすべての祖先**。信頼は git のルート単位（実測）。git を実行してルートを探すより単純で確実。祖先の指示ファイルは既存の検査（`ancestor_instructions`）で実行を止めるので、祖先を信頼しても余計なものは読み込まれない |
| 読み込みの観測（Codex） | (a) 何もしない (b) 試行の前に `codex debug prompt-input` と `codex mcp list --json` を同じ環境で実行 (c) 試行の出力から推定 | **(b)**。費用 0・約 2 秒。モデルに渡す入力そのものなので確実。旧版などで失敗したら「観測できない」と記録するだけ |
| 読み込まれなかったときの扱い | (a) 試行を無効にする (b) 記録して集計・レポートで知らせる | **(b)**。配布物の誤り（frontmatter の不備など）で読み込まれないのは「配布物の失敗」として測るべきで、環境起因と自動では区別できない。gate で止める選択肢はフェーズ 4 で加える |
| 指標の追加 | 呼び出し率・誤発火率・pass@k・区間 | **すべて加える（追加のみ）**。既存の指標・Δ の定義は変えない（gate・promote の互換性） |
| タスクの形式 | (a) SkillsBench 形式に乗り換える (b) YAML を保ち、取り込み（変換）を作る | **(b)**。YAML の過程グレーダー（`tool_used` 等）・`split`・`family`・`paired_negative` は SkillsBench 形式にない。取り込みは変換して YAML を書き出す（フェーズ 3） |
| アダプタ | (a) このリポジトリで保守を続ける (b) 共有ライブラリ `agent_cli` を複製して同梱 | **(b)**（フェーズ 2）。wikiskill と同じ処理（版ごとのコマンド・ストリームの解析・上限の抽出）を 1 か所で実測し直せる。依存は増やさない |
| 版 | 0.9.0（後方互換）／1.0.0（破壊的） | **0.9.0**。キー・引数・形式は追加だけ。測定値の変わるプロファイル（プロジェクト単位の配布物）は CHANGELOG で取り直しを案内する |

## 6. 目標アーキテクチャ

```
                         ┌──────── agent_cli（複製して同梱。CLI ごとの差をすべて持つ）────────┐
                         │ probe_version / build_command / parse_stream / classify_error /      │
                         │ extract_usage / seed_trust / observe_loaded                          │
                         └──────────────────────────────▲───────────────────────────────────────┘
tasks (YAML ← import: SkillsBench/skilljack)            │
profiles (YAML) ─ spec ─ runner ── trial ─ isolation ───┤
                                    │       effective（期待・観測・差・記録）
                                    ├─ graders（成果物・過程・LLM）
                                    └─ record（TrialRecord + effective）
                         metrics（pass@1/pass^k/pass@k・区間・lift・呼び出し率・誤発火率・読み込みの検査）
                         → gate / promote / report
```

### 6.1 試行の流れ（0.9.0）

1. フィクスチャの複製 → 環境変数 → 認証ファイルの複製 → `install`（0.8.0 と同じ）
2. **信頼の付与**（`trust_workdir: true` のとき）: Claude Code は `{config_dir}/.claude.json` の `projects[<パス>].hasTrustDialogAccepted = true` を
   作業ディレクトリとすべての祖先に（既存の内容に統合）。Codex は `{config_dir}/config.toml` の末尾に `[projects."<workdir>"] trust_level = "trusted"`
   （同じ表が既にあれば書かない）。Windows では区切りを `/` にした形も書く（未実測。§10 フェーズ 4 で確認）
3. **期待する部品の抽出**: 置いたファイルから、CLI の探索場所にあるスキル・サブエージェント・MCP・常時指示を集める（§6.2）
4. **観測**（Codex のみ起動前）: `codex debug prompt-input` と `codex mcp list --json` を試行と同じ環境・作業ディレクトリで実行
5. 起動 → 解析（0.8.0 と同じ）。Claude Code の観測は `system/init`
6. **照合と記録**: 期待 − 観測 = 読み込まれなかった部品。標準エラーの信頼の警告も拾う。`effective_config.json` とレコードの `effective` に書く
7. 異常終了の分類（`usage_limit` を追加）→ 採点 → 後始末

### 6.2 有効設定の記録（`effective_config.json`・`TrialRecord.effective`）

試行フォルダ（`<run>/trials/<task>/<profile>/<n>/effective_config.json`）と、`trials.jsonl` の各レコードの `effective` に同じものを書く。
値は**秘密を含めない**（環境変数は名前だけ、認証ファイルは記録しない）。

```json
{
  "schema": 1,
  "agent": "claude_code",
  "cli_version": "2.1.325 (Claude Code)",
  "argv": ["claude", "-p", "--output-format", "stream-json", "..."],
  "isolate_home": true,
  "config_env": "CLAUDE_CONFIG_DIR",
  "env_set": ["CLAUDE_CODE_USE_BEDROCK"],
  "env_unset": ["CLAUDECODE", "..."],
  "trust": {"enabled": true, "paths": ["<workdir>", "<祖先>", "..."]},
  "installed": {"home/.claude/skills/x/SKILL.md": "<sha256 先頭 16 桁>", "workdir/.mcp.json": "..."},
  "expected": {"skills": ["x"], "agents": [], "mcp_servers": [], "instructions": ["home/.claude/CLAUDE.md"]},
  "observed": {"source": "init", "skills": ["x", "..."], "agents": ["..."], "mcp_servers": ["..."], "instructions": null},
  "missing": {"skills": [], "agents": [], "mcp_servers": [], "instructions": []},
  "misplaced": ["home/.claude.json: CLAUDE_CONFIG_DIR を設定すると読まれません（{config_dir}/.claude.json に置く）"],
  "warnings": ["Ignoring 1 permissions.allow entry ..."]
}
```

- `installed`: 一時ホームのすべてのファイル（認証ファイル `.credentials.json`・`auth.json` を除く）と、作業ディレクトリで `install` が足した・変えたファイル
  （フィクスチャを除く）。パスは試行フォルダからの相対（`/` 区切り）
- `expected`（CLI ごとの探索場所。§2 の実測に合わせる）

| 種類 | Claude Code | Codex |
|---|---|---|
| skills | `{config_dir}/skills/*/SKILL.md`、`{workdir}/.claude/skills/*/SKILL.md` | `{config_dir}/skills/*/SKILL.md`、`{home}/.agents/skills/*/SKILL.md`、`{workdir}/.agents/skills/*/SKILL.md` |
| agents | `{config_dir}/agents/*.md`、`{workdir}/.claude/agents/*.md` | （対象外） |
| mcp_servers | `{config_dir}/.claude.json` と `{workdir}/.mcp.json` の `mcpServers` のキー | `{config_dir}/config.toml` と（信頼時）`{workdir}/.codex/config.toml` の `[mcp_servers.<名前>]` |
| instructions | `{config_dir}/CLAUDE.md`、`{workdir}/CLAUDE.md`、`{workdir}/.claude/CLAUDE.md`（観測手段がないので照合しない） | `{config_dir}/AGENTS.md`（または `AGENTS.override.md`）、`{workdir}/AGENTS.md`（prompt-input に本文があるかで照合） |

  スキルの名前は frontmatter の `name:`、なければフォルダ名。サブエージェントは frontmatter の `name:`、なければファイル名。
- `observed`: Claude Code は `system/init`（`skills`・`agents`・`mcp_servers[].name`）。Codex は prompt-input のスキル一覧と `mcp list` の名前、
  常時指示は prompt-input に各ファイルの先頭の非空行が含まれるか。観測できなければ `source: null` で、`missing` は計算しない（空ではなく `null`）
- `missing` が空でない試行は、集計の `harness_load`（§6.3）とレポートの警告に出す。試行の有効・無効は変えない

### 6.3 指標層

既存の定義（pass@1・pass^k・比較用の値・Δ・gate・promote）は**変えない**。次を追加する。

| 指標 | 定義 | 区間 | 置き場所 |
|---|---|---|---|
| `pass_at_k`（タスク） | 有効試行 n・合格 c のとき `1 − C(n−c, k) / C(n, k)`（不偏推定。Chen et al. 2021）。n < k は None | — | `summary.tasks[p][t]` |
| `pass_at_k`（プロファイル） | タスク平均 | — | `summary.profiles[p]` |
| `pass_at_1_ci` | タスクごとの pass@1 を復元抽出したブートストラップ 95% 区間（試行を独立に数えない。Δ と同じ方法） | 2 タスク以上 | `summary.profiles[p]` |
| `trigger.invocation_rate` | **陽性**（`skill_used` で `negate` なし・`min ≥ 1`）の判定を持つ有効試行のうち、対象のスキルを呼んだ割合（判定ごとに 1 件。合否とは独立） | Wilson 95% | `summary.profiles[p].trigger` |
| `trigger.false_trigger_rate` | **否定の対照**（`skill_used` で `max: 0`、または `negate: true`）の判定を持つ有効試行のうち、対象のスキルを呼んだ割合 | Wilson 95% | 同上 |
| `trigger.detection` | `exact`（Claude Code の Skill ツール）／`heuristic`（Codex の SKILL.md 読み込みの推定）。混在すれば `mixed` | — | 同上 |
| スキルの効果（lift） | 既存の `deltas(summary, baseline)` の `pass_at_1`（比較用の値）とその 95% 区間 | 既存 | `report` の表記を「lift」と併記 |
| `harness_load` | `{checked, with_missing, missing: {skills: {名前: 試行数}, ...}}`。観測できた試行だけ数える | — | `summary.profiles[p]` |

- 呼び出し率は「タスクの判定」単位で数える（1 試行に陽性の判定が 2 つあれば 2 件）。判定の対象（スキル名）と向きは、グレーダーの結果に
  `target`・`trigger`（`positive`／`negative`）として残す（`grader_results` への追加）
- 否定の対照の発火は、合否とは別に数える（タスクの合否は既存の `skill_used max: 0` の判定がそのまま担う）
- Codex で `skill_detection: heuristic` の試行も数えるが、`detection` で区別して表示する。「不明」を「呼ばなかった」と数えない原則（agent-skill-eval）に合わせ、
  将来 Codex の観測が厳密になったら `exact` に変わる

### 6.4 タスク形式と取り込み（フェーズ 3）

YAML（現行の形式）を正とし、SkillsBench／skilljack の `task.md` 形式を**変換して取り込む**。変換の対応は §10 フェーズ 3。

### 6.5 アダプタ層（フェーズ 2）

`harness_eval/adapters.py` の CLI 固有の処理を、複製して同梱する `agent_cli` へ移す（§7）。このリポジトリに残すのは、プロファイル・タスクから
`agent_cli` の呼び出し引数を作る薄い層と、`ParsedRun` への変換だけ。
