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
