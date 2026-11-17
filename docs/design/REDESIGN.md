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

## 7. agent_cli に求めるもの

`agent_cli` は wikiskill と agent-harness-eval が共有する、Claude Code／Codex の CLI を扱うライブラリ。**標準ライブラリだけ**で書き、
各リポジトリに**複製して同梱**する（`harness_eval/_vendor/agent_cli/`。インストールの依存は増やさない）。このリポジトリが必要とするものを、
入力・出力・不変条件の形で列挙する。名前は案で、wikiskill 側の設計書と突き合わせて確定する。

### 7.1 全体の約束

- 純粋な関数とデータクラスで構成する。**プロセスの起動・タイムアウト・プロセスツリーの停止・一時ホームの作成は呼び出し側**（このリポジトリの `isolation.py`）が持つ。
  `agent_cli` は「何を起動するか」と「出力をどう読むか」だけを持つ（評価ツールは試行の隔離と費用の記録を自分で制御する必要があるため）
- 例外: `probe_version`・`observe_loaded` のように CLI を短く起動する関数は、`runner: Callable[[list[str], dict[str, str], Path], tuple[int, str, str]]`
  （argv・env・cwd → 終了コード・stdout・stderr）を引数で受け取る（テストで偽の CLI に差し替えるため）
- 未知のイベント・未知のキーは無視する（版上げで壊れない）。解析できない行は数えて警告にする
- 実測した版を `VERIFIED = {"claude_code": "2.1.325", "codex": "0.169.0"}` のように持ち、フラグの選択は**版の範囲**で決める（§7.2）
- Python 3.10 以上（`tomllib` は 3.11 からなので TOML は使わない／最小限の正規表現で読む）

### 7.2 版の検出とコマンドの組み立て

```python
@dataclass(frozen=True)
class CliVersion:
    agent: str                 # "claude_code" | "codex"
    raw: str | None            # `--version` の 1 行目
    parsed: tuple[int, int, int] | None

def probe_version(agent: str, exe: list[str], runner) -> CliVersion
def older_than_verified(v: CliVersion) -> bool | None

@dataclass(frozen=True)
class RunSpec:                 # エージェント非依存の「何をさせるか」
    workdir: Path
    model: str | None = None
    max_turns: int | None = None             # Claude Code のみ
    max_budget_usd: float | None = None      # Claude Code のみ
    allow_tools: tuple[str, ...] = ()        # Claude Code: --allowedTools
    deny_tools: tuple[str, ...] = ()         # Claude Code: --disallowedTools
    permission_mode: str | None = None
    sandbox: str | None = None               # Codex: -s
    settings: dict | None = None             # Claude Code: --settings（JSON）
    include_hook_events: bool = False
    bypass_hook_trust: bool = True           # Codex: --dangerously-bypass-hook-trust（版にあれば）
    ephemeral: bool = True                   # Claude Code: --no-session-persistence / Codex: --ephemeral
    reasoning_effort: str | None = None      # Codex: -c model_reasoning_effort=...（記録用にも使う）
    extra_args: tuple[str, ...] = ()

def build_command(agent: str, exe: list[str], spec: RunSpec, version: CliVersion) -> list[str]
def build_judge_command(agent: str, exe: list[str], model: str | None, version: CliVersion) -> list[str]
```

- プロンプトは**常に標準入力**（Windows の引数長と引用符の問題を避ける。両 CLI で確認済み）
- 版にないフラグは付けない（例: Codex 0.163 未満の `--dangerously-bypass-hook-trust`）。付けなかったフラグは `build_command` の戻り値と別に
  `dropped_flags: list[str]` として返す（有効設定の記録に入れる）。形: `CommandPlan(argv: list[str], dropped_flags: list[str])`
- このリポジトリで使うフラグ（現行の `adapters.py:92-101`, `:226-234`）はすべて表現できること

### 7.3 ストリームの解析

```python
@dataclass
class ToolCall: name: str; input: Any; is_error: bool | None = None; from_subagent: bool = False

@dataclass
class StreamResult:
    final_message: str | None
    tool_calls: list[ToolCall]
    skill_calls: list[str];  skill_detection: str        # "exact" | "heuristic"
    subagent_calls: list[str]
    hook_calls: list[dict] | None                         # [{name, exit_code, outcome}]。記録がなければ None
    loaded: Loaded | None                                 # init で分かった読み込み（§7.6）
    usage: Usage
    num_turns: int | None; duration_ms: int | None
    error: CliError | None                                # §7.5
    meta: dict                                            # thread_id, terminal_reason, api_retries, network_stalled など
    warnings: list[str]

def parse_stream(agent: str, lines: Iterable[str]) -> StreamResult
def parse_judge(agent: str, stdout: str) -> tuple[str | None, float | None, CliError | None]
```

- Claude Code: `system/init`・`system/hook_response`・`system/api_retry`・`assistant`（`parent_tool_use_id` でサブエージェントを区別、`Skill`・`Agent`／`Task` のツール）・
  `user`（`tool_result.is_error`）・`result`。現行 `adapters.py:107-200` の挙動をそのまま移す（テストデータ `tests/data/claude_stream_real.jsonl`）
- Codex: `thread.started`・`item.completed`（`agent_message`・`command_execution`・`file_change`・`mcp_tool_call`・`collab_tool_call`・`web_search`・`error`）・
  `turn.completed`・`turn.failed`・`error`（再接続）。初期版の `item_type` も読む。現行 `adapters.py:239-310`（`tests/data/codex_failed_real.jsonl`）
- `--dangerously-bypass-hook-trust` の既知の通知（`item.completed` の `error`）は警告にしない

### 7.4 使用量・費用・上限の抽出

```python
@dataclass
class Usage:
    input: int | None; output: int | None; cache_read: int | None; cache_write: int | None
    reasoning_output: int | None = None
    cost_usd: float | None = None; cost_source: str | None = None   # "client_estimate" | None
    context_tokens: int | None = None   # 本体の最初のリクエストの入力側の合計（キャッシュの状態によらない）
    input_includes_cache: bool = False  # Codex: cached は input の内数（OpenAI の定義）
```

- 費用は CLI が出すときだけ（Claude Code の `total_cost_usd`）。Codex の単価換算はこのリポジトリ（`pricing`）で行う（ライブラリに単価表を持たせない）
- 上限の情報（§7.5 の `reset_hint`）を取り出す

### 7.5 エラーの分類

```python
@dataclass(frozen=True)
class CliError:
    kind: str            # 下の表
    message: str         # 500 文字まで
    http_status: int | None = None
    reset_hint: str | None = None   # 「try again at …」「resets …」の部分（原文のまま。解釈しない）
    retryable: bool = False

def classify_error(agent: str, *, result_event: dict | None, message: str | None, stderr: str | None) -> CliError | None
```

| kind | 意味 | 例・根拠 | このリポジトリの扱い |
|---|---|---|---|
| `rate_limit` | 短期のレート制限 | HTTP 429、`rate limit`、`too many requests` | 除外・再試行 |
| `overloaded` | 混雑 | HTTP 529、`overloaded` | 除外・再試行 |
| `usage_limit` | プラン・残高の上限（長い待ちが必要） | Codex 0.169: `You’ve hit your usage limit. … try again at …`（実測）。`credit balance is too low`、`insufficient_quota`、`hit your limit` | 除外・**再試行しない** |
| `auth` | 認証 | 401・403、`Not logged in`、`invalid api key` | 除外 |
| `api_error` | その他の API エラー（400 など） | `api_error_status ≥ 400` | 除外 |
| `max_turns` / `budget_exceeded` | CLI 側の上限 | `error_max_turns`・`error_max_budget_usd` | 失敗として数える |
| `agent_crash` | 結果イベントがない | — | 失敗（stderr で上を判定できれば上） |
| `agent_error` | 上のどれでもない | — | 失敗 |

- 分類の正規表現は 1 か所に置き、実測した文面をテストデータとして同梱する（`tests/data/` 相当）

### 7.6 読み込みの観測と信頼の付与（このリポジトリで最も重要）

```python
@dataclass
class Loaded:
    source: str                         # "init" | "prompt_input"
    skills: list[str]; agents: list[str]; mcp_servers: list[str]; plugins: list[str]
    instructions_text: str | None       # Codex: AGENTS.md の本文（照合用）。Claude Code は None

def observe_loaded(agent: str, exe: list[str], env: dict[str, str], cwd: Path, runner, version: CliVersion) -> Loaded | None
    # Codex: `codex debug prompt-input x` と `codex mcp list --json`（費用 0）。Claude Code: None（init で分かる）
def parse_prompt_input(text: str) -> Loaded | None
def trust_files(agent: str, config_dir: Path, workdir: Path) -> list[tuple[Path, str]]
    # 書くべきファイルと内容（既存の内容と統合した結果）。書き込みは呼び出し側
def discovery_locations(agent: str) -> dict[str, list[str]]
    # §6.2 の表（{config_dir}・{home}・{workdir} のプレースホルダ付き）
def trust_warnings(agent: str, stderr: str) -> list[str]
    # 「this workspace has not been trusted」などの行
```

- 探索場所・信頼の書式は CLI の版で変わりうるので、`agent_cli` 側で版ごとに持つ（実測した版を注記）
- wikiskill も「育てたスキルが実際に読み込まれるか」の確認に同じ関数を使える

### 7.7 このリポジトリ側に残すもの

プロファイル・タスクから `RunSpec` を作る、`StreamResult` を `ParsedRun`／`TrialRecord` に写す、`pricing` による費用の換算、期待する部品の抽出
（`install` の結果を見る。§6.2）、照合と記録、プロセスの実行と隔離。

## 8. 移行計画（既存のタスク・結果）

0.9.0 は後方互換なので、**変換は不要**。

| 対象 | 0.9.0 での扱い |
|---|---|
| タスクの YAML | そのまま読む（キーの追加なし） |
| プロファイルの YAML | そのまま読む。新しいキー `trust_workdir`（既定 `true`）は任意 |
| 過去の `trials.jsonl`・`summary.json` | そのまま `report`・`gate --previous`・`promote` に使える。`TrialRecord.from_dict` は未知のキーを無視し、ないキーは既定値になる（`effective` は `None`）。新しい指標（`trigger`・`pass_at_k`・`harness_load`）は過去の結果では `None`／空 |
| キャッシュ（`.harness-eval-cache/`） | `CACHE_SCHEMA` を 6 に上げ、ツール版もキーに入るので、過去のキャッシュは使われない（全試行を取り直す。中身を消す必要はない） |
| `gate --previous`（0.8.0 の結果） | 比べられる。ただし**プロジェクト単位の配布物を持つプロファイル**は 0.9.0 で測定値が変わるので、gate は前回の結果に有効設定の記録がないとき注意を出す。基準の結果を 0.9.0 で取り直してから gate する |
| holdout の利用台帳 | 形式は変えない |

後続のフェーズで形式を変える場合（例: タスクの `schema` キーの導入）は、そのフェーズで 1.0.0 にし、`docs/MIGRATION.md`（日本語）と
`harness-eval migrate` を同時に入れる（§10 の各フェーズに条件を書く）。

## 9. 今回（0.9.0）の実装範囲

| 項目 | 内容 | 場所 |
|---|---|---|
| 信頼の付与 | `trust_workdir`（既定 `true`）。Claude Code: `.claude.json` の統合、Codex: `config.toml` への追記 | `harness_eval/effective.py`、`spec.py`、`runner.py` |
| 有効設定の記録 | `effective_config.json`・`TrialRecord.effective`（§6.2） | `effective.py`、`record.py`、`runner.py` |
| Codex の読み込みの観測 | `codex debug prompt-input` と `codex mcp list --json` | `adapters.py`（`CodexAdapter.observe_cmds`・`parse_prompt_input`）、`effective.py` |
| 利用上限の除外 | `usage_limit`（除外・再試行しない・`reset_hint`） | `adapters.py`、`record.py` |
| 指標 | `pass_at_k`、`pass_at_1_ci`、`trigger`（呼び出し率・誤発火率・Wilson 区間）、`harness_load` | `metrics.py`、`graders.py`（`target`・`trigger`） |
| 表示 | レポートの列と警告、run の終了時の警告、gate の注意 | `report.py`、`cli.py` |
| CI | `.github/workflows/test.yml`（push・pull_request、ubuntu/macos/windows × 3.10/3.13、uv、pytest、ruff は 1 セル、アクションは SHA 固定） | `.github/` |
| 版 | 0.9.0、`CHANGELOG.md` | `pyproject.toml`、`harness_eval/__init__.py`、`uv.lock`、README |

### 9.1 実装の結果と検証（2026-11-17）

- 新しいモジュール `harness_eval/effective.py`（信頼の付与・期待／観測・照合・記録）。`runner._execute_in` は install の後に信頼を付け、
  Codex は起動前に観測し、試行の後に `effective` を書く。`doctor --live` は読み込まれなかった部品を NG として出す
- 偽の CLI（`tests/fake_agents/`）に、§2 の実測の挙動（init のユーザー・プロジェクトの部品、未信頼の警告、`debug prompt-input`・`mcp list --json`、
  プロジェクトの `.codex/config.toml` の信頼、利用上限の文面）を足し、`tests/test_effective.py`（23 件）で確かめた
- CI を足したところ、**Windows で一度も動かしていなかった既存のテストが 6 件失敗**した。うち 1 件は本物の不具合
  （`harvest._safe_rel` が Windows で `/etc/x` を作業ディレクトリ内と誤認。修正済み）、残りはテストの書き方（改行の変換・ドライブの書式・
  パスの埋め込み・chmod）。修正後、ubuntu・macOS・Windows × 3.10・3.13 の 6 セルすべて成功
- 実機の CLI での確認は §2 のプローブまで（Claude Code は未ログイン、Codex は利用上限のため、モデルを呼ぶ試行は行っていない）。
  モデルを呼ぶ確認（`allowed-tools` の付与が `-p` で効くこと、信頼の付与で `permissions.allow` が実際に効くこと）は、認証のある環境で
  `doctor --live` と同梱の例で行う（フェーズ 4 の最初の作業）

## 10. 後続のフェーズ（別の担当が実装する。各フェーズは独立した PR）

共通の約束: 既存のテストを弱めない・消さない。偽の CLI（`tests/fake_agents/`）で再現できない挙動は、実機の出力をテストデータとして `tests/data/` に置く。
版の規則（後方互換なら minor、壊すなら major + `docs/MIGRATION.md` + 変換コマンド）と CHANGELOG の「アップグレード方法」を守る。

### フェーズ 2: `agent_cli` の同梱とアダプタの置き換え（1.0.0 にはしない）

- 前提: wikiskill 側の設計書と §7 を突き合わせて `agent_cli` の API を確定し、上流（共有の場所）で実装・テストされていること
- 手順:
  1. `harness_eval/_vendor/agent_cli/` に複製（`__init__.py` に上流のコミットと複製日を書く。手で直さない。直すときは上流を直して複製し直す）
  2. `harness_eval/adapters.py` を薄い層にする: `ClaudeAdapter.build_cmd` → `RunSpec` を作って `agent_cli.build_command`、`parse` → `agent_cli.parse_stream` の結果を `ParsedRun` に写す、
     `_classify_message`・`_status_type`・`classify_stderr` → `agent_cli.classify_error`、`CodexAdapter.observe_cmds`・`parse_prompt_input` → `agent_cli.observe_loaded`、
     `effective.seed_trust` の書式 → `agent_cli.trust_files`
  3. `VERIFIED_VERSIONS`・`older_than_verified` は `agent_cli` のものを使う
  4. `pyproject.toml` の `packages` に `harness_eval._vendor`, `harness_eval._vendor.agent_cli` を足す（依存は増やさない）
- 受け入れ条件: 既存のテストがすべて通る（特に `tests/test_units.py` の解析・分類のテスト、`tests/data/*.jsonl`）。起動コマンドが 0.9.0 と同じ（偽の CLI の `FAKE_ARGV_LOG` で比較するテストを追加）
- 形式の変更なし → minor（0.10.0）

### フェーズ 3: SkillsBench／skilljack 形式のタスクの取り込み（`harness-eval import`）

- 新しいモジュール `harness_eval/importer.py`、CLI `harness-eval import --format {skilljack,skillsbench} <入力フォルダ> --out <出力フォルダ> [--agent claude_code|codex ...]`
- 出力: `<out>/tasks/<id>.yaml`、`<out>/fixtures/<id>/`（作業フォルダの種）、`<out>/checks/<id>/`（検証スクリプト）、`<out>/profiles/<id>-with-skill-<agent>.yaml`
  と `<out>/profiles/baseline-<agent>.yaml`（スキルの有無のペア）、`<out>/IMPORT_REPORT.md`（変換できなかった項目）
- 対応（skilljack `task.md`。README の記述に基づく。実装時に上流のスキーマで確認する）:

| 入力 | 出力 |
|---|---|
| `task.md` の frontmatter `id`（なければフォルダ名） | `id` |
| `task.md` の本文 | `prompt` |
| `environment/workspace/` | `fixture` |
| `environment/skills/<名前>/` | プロファイルの `install: [{copy: ..., to: "{config_dir}/skills/<名前>"}]`（Codex は `{home}/.agents/skills/<名前>`） |
| `expected_skill` + `expect_skill_invocation: true`（既定） | `skill_used: {skill: <名前>, min: 1}`（陽性） |
| `expect_skill_invocation: false`（anti-trigger） | `skill_used: {skill: <名前>, max: 0, category: skill_misfire}`（否定の対照）。陽性のタスクと `paired_negative` で結ぶ |
| `checks:` の `files_exist` 等 | `file_exists`・`file_contains`・`regex` に対応するものだけ。対応しないものは `IMPORT_REPORT.md` |
| `verifier/<script>` | `command` グレーダー: `{python} {task_dir}/../checks/_reward.py <verifier>`。`_reward.py`（同梱）が `SKILLJACK_OUTPUT_FILE`（最終応答）・`SKILLJACK_TRAJECTORY_FILE`（ツール呼び出しの JSON）・`SKILLJACK_TASK_DIR`・`SKILLJACK_REWARD_FILE` を用意して検証スクリプトを実行し、報酬 ≥ 1.0（なければ終了コード 0）で終了コード 0 |
| `oracle/solve.*` | `harness-eval validate --oracle`（新オプション）で、作業フォルダに解答を当てて採点が全合格になるか確かめる |
| SkillsBench（Harbor）`instruction.md`・`environment/`・`tests/test.sh`（`/logs/verifier/reward.txt`） | `prompt`・`fixture`・`command`（`bash` があるときだけ。Windows では `IMPORT_REPORT.md` に「実行不可」）。`environment/Dockerfile` は対応しない（報告だけ） |

- `command` グレーダーは今は合否だけ。報酬が 0〜1 の小数の場合は、`reason` に報酬を残し、合否は報酬 ≥ 1.0 とする（スコアの部分点はフェーズ 5 の検討事項）
- 取り込みは**書き出すだけ**で、既存の YAML の読み込みは変えない → minor
- テスト: `tests/data/import/skilljack-min/`（陽性・anti-trigger・verifier・oracle を 1 つずつ）を変換し、`validate` が警告なしで通り、偽の CLI で `run` できる

### フェーズ 4: 読み込みの検査を関門に入れる・費用 0 の doctor・警告

- `gate`／`promote` に `--require-harness-load`（既定は無効）: 候補のプロファイルに `harness_load.with_missing > 0` があれば `fail`（gate）／`reject`（promote）。
  理由に「読み込まれなかった部品」を出す
- `doctor`（`--live` なし）に費用 0 の読み込み確認を足す: プロファイルごとに一時の試行フォルダを作って `install` と信頼の付与を行い、
  Claude Code は未ログインでも出る `system/init`（`--max-turns 1`、標準入力 `x`。§2.1）を、Codex は prompt-input を読んで、
  `effective.expected` と照合して表示する。**注意:** Claude Code はログイン済みだと API を 1 回呼ぶので、`ANTHROPIC_API_KEY=invalid` のような無効な認証を
  わざと渡す方式は**採らない**（口座の経路を変えるため）。代わりに `--max-budget-usd 0.0001` で 1 回目の応答前に止まるかを実測で確かめ、だめなら `--live` 側にだけ入れる
- `validate` の警告: (1) `install` の先が `{home}/.claude.json`（Claude Code。§2.2 で読まれないと実測）、(2) 配布物の `settings.json` が `sandbox.enabled: true` で、
  タスクの `allow_tools` に `Bash` がない（2.1.325 から確認＝`-p` では拒否）、(3) `trust_workdir: false` でプロジェクト単位の設定ファイルを置いている
- Windows: 信頼の付与のキー（Claude Code の `projects` のパスの書式、Codex の `[projects."C:\\..."]`）を実機で確かめ、`effective.seed_trust` を直す。CI の windows セルで単体テストは動くが、実機の確認は手で行い結果をこの文書の §2 に追記する
- Claude Code の常時指示（CLAUDE.md）の観測: `InstructionsLoaded` の hook を `--settings` 経由で足すと配布物の hooks と混ざるため、**採らない**。
  代わりに `context_tokens` の基準との差（既存の `deltas.context_tokens`）で間接的に見る
- 形式の追加だけ → minor

### フェーズ 5: 指標の発展（任意）

- 呼び出し率・誤発火率の**基準との差と区間**（タスク単位のブートストラップ）と、promote の目的に `--objective trigger`（誤発火率を上げずに呼び出し率を上げる）
- 試行ごとのトークン・時間の上限（agent-skill-eval の budget guard: `warn`／`fail`／`stop-suite`）をタスクの `limits` に `max_tokens` として追加
- `command` グレーダーの部分点（報酬 0〜1 をスコアに反映）。スコアの定義が変わるので、比較用の値と gate への影響を設計してから

## 11. 参考資料・プローブ

- OSS: https://github.com/olaservo/skilljack-evals ・ https://github.com/tardigrde/agent-skill-eval ・ https://github.com/UiPath/coder_eval ・ https://github.com/cskwork/skill-ab-eval
- Claude Code の公式文書（2026-11 時点の写し）: headless（`-p` は信頼の確認を出さない）、permissions「Project allow rules and workspace trust」「What runs before you trust a folder」、
  skills「Pre-approve tools for a skill」、settings。変更履歴 2.1.325（サンドボックスの Bash の確認、scheduled tasks と `/loop` のスキルの確認）
- `codex exec --help`（0.169.0）: `--trust`・`--ignore-user-config`・`--dangerously-bypass-hook-trust`
- 実測のプローブ（このリポジトリには含めない。手順を残す）:
  - Claude Code: 一時ホームに `{config_dir}/skills`（`allowed-tools`・`hooks` 付きを含む）・`agents`・`.claude.json`（MCP）・`{home}/.claude.json`（MCP）、
    作業ディレクトリに `.claude/{skills,agents,settings.json}`・`.mcp.json`・`CLAUDE.md` を置き、`CLAUDECODE` などホストの変数を外して
    `claude -p --output-format stream-json --verbose --model haiku --max-turns 2 --no-session-persistence` を未ログインで実行し、`system/init` と標準エラーを見る。
    MCP サーバーは起動されると印のファイルを作る Python の 1 行にする。信頼は `{config_dir}/.claude.json` の `projects[<path>].hasTrustDialogAccepted`。
    親の git リポジトリの場合は試行フォルダの親で `git init`
  - Codex: 一時の `CODEX_HOME` に `auth.json` を複製し、`AGENTS.md`・`skills/`・`config.toml`（`[mcp_servers.*]`）、`{home}/.agents/skills`、
    作業ディレクトリに `AGENTS.md`・`.agents/skills`・`.codex/config.toml`（`developer_instructions` と MCP）を置き、`codex debug prompt-input hello`・
    `codex mcp list --json`・`codex exec --json --skip-git-repo-check --ephemeral -C <wd> -s workspace-write -c model_reasoning_effort="low" -` を実行する
