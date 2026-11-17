# 変更履歴

この形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に従い、版は [セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## [0.9.0] - 2026-11-17

試行が候補のハーネスを確実に読み込むための対策と、試行ごとの有効設定の記録、スキルの発火の指標を加えました。
背景と実測の結果は [`docs/design/REDESIGN.md`](docs/design/REDESIGN.md) にあります（Claude Code 2.1.325・codex-cli 0.169.0 で確認）。

### 追加

- **作業ディレクトリの信頼**（プロファイルの `trust_workdir`、既定 `true`）: 隔離した設定の中で、試行の作業ディレクトリを信頼済みにしてから起動する。
  未信頼のフォルダでは、Claude Code は `.claude/settings.json` の `permissions.allow` を、Codex は `.codex/config.toml` 全体（指示・MCP）を黙って使わないため
- **試行ごとの有効設定の記録**: 試行フォルダの `effective_config.json` と `trials.jsonl` の `effective`。起動コマンド（秘密の値は伏せる）、
  環境変数の名前、信頼を付けたパス、置いたファイルのハッシュ、期待した部品（スキル・サブエージェント・MCP・常時指示）、
  CLI が実際に読み込んだ部品（Claude Code は `system/init`、Codex は費用 0 の `codex debug prompt-input`・`codex mcp list --json`）、読み込まれなかった部品
- **読み込みの検査**: 置いた部品が読み込まれなかった試行の数を `summary.json` の `harness_load` に出し、`run` の終了時とレポートで知らせる
- **指標**: `pass_at_k`（少なくとも 1 回合格する確率の不偏推定）、`pass_at_1_ci`（タスク単位のブートストラップ 95% 区間）、
  `trigger`（スキルの呼び出し率と、否定の対照での誤発火率。Wilson の 95% 区間付きで、合否と切り離して数える）。レポートに「反復・スキルの発火・読み込み」の表
- `skill_used` の判定の結果に `target`（スキル名）と `trigger`（`positive`／`negative`）を記録する
- テストの CI（`.github/workflows/test.yml`: ubuntu・macOS・Windows × Python 3.10・3.13）

### 変更

- 利用上限（「You’ve hit your usage limit」・残高不足など）を `usage_limit` として**集計から除外**する（これまではエージェントの失敗として数えていた）。
  再試行はせず、戻る時刻の文面を `error.reset_hint` に残す。Claude Code で HTTP 429 と上限の文面が両方あるときも `usage_limit` にする
- キャッシュの形式を 6 に上げた（過去のキャッシュは使われず、取り直す）
- `gate` は、前回の結果が 0.8.0 以前（信頼を付けずに測った結果）のとき注意を出す
- `doctor --live` は、置いたのに読み込まれなかった部品を NG として表示する

### 修正

- `harvest` の下書きのファイルのパスで、Windows では `/etc/x` のような根から始まるパスを作業ディレクトリ内と誤って許していた
  （ドライブの根に書かれる）。`\x`・`C:x`・どちらの区切りの `..` も拒む
- テストを Windows でも通るようにした（改行の変換・ドライブの書式・パスの埋め込み・読めないファイルの作り方）

### アップグレード方法

1. いつもの方法で更新する（`git pull` または ZIP を展開し直し、`pip install -e .` または `uv sync`）。`harness-eval --version` が `0.9.0` なら完了
2. タスク・プロファイルの YAML、過去の `trials.jsonl`・`summary.json`、holdout の台帳は**そのまま使える**（変換は不要）。過去の結果では新しい指標は `—` になる
3. **作業ディレクトリ（`{workdir}`）に `.claude/` や `.codex/` を置くプロファイル**は、信頼の付与で測定値が変わる（読み込まれていなかった設定が効くようになる）。
   `gate --previous` で比べる前に、基準の結果を 0.9.0 で取り直す。未信頼のときの挙動を測りたい場合だけ、プロファイルに `trust_workdir: false` を書く
4. `{home}/.claude.json` に MCP を置いているプロファイルは、Claude Code では読まれていない（`CLAUDE_CONFIG_DIR` を設定すると読まれるのは `{config_dir}/.claude.json`）。
   置き先を `{config_dir}/.claude.json` に直す。0.9.0 の `effective_config.json` の `misplaced` に出る

## [0.8.0] - 2026-11-04

### 追加

- 効率（毎回読み込まれる量＝開始時の文脈、成功あたりの費用・トークン）の計測と、`promote --objective efficiency`（成功率を落とさず費用を下げた変更案の採用）
- `improve --goal efficiency`（配布物を軽くする変更案）

## [0.7.0] - 2026-11-02

### 追加

- `improve` が事前確認で不合格の案を 1 回だけ指摘付きで作り直す。API キーでの認証の手順

### 修正

- エージェントが読まないスキルのファイルを含む変更案を拒否する。ホストのセッションの変数を試行に渡さない。却下した案の本文を `improve.json` に残さない

## [0.6.0] - 2026-10-31

### 追加

- 改善役（`improve`）: train の失敗から読み取りだけの `claude -p` が配布物の変更案を作る。holdout を権限設定で読ませないことを起動のたびに確かめる
- 事前確認、スキルの 3 つの形（なし／説明文だけ／説明文＋本文）を作る `skill-variants`

## [0.5.0] - 2026-10-31

### 追加

- 自己改善ループの評価側の土台: タスクの分割（`split`: train・dev・holdout、`family`）、holdout の利用台帳、`promote`（候補 vs 現行、評価した候補の数で有意水準を補正）、
  `gate --max-regressions`（回帰予算）

## [0.4.0] - 2026-10-28

### 追加

- 統計的なリリース判定（タスク単位のブートストラップ区間と非劣性マージンによる 通過／不通過／判定不能）、比較用の値（配布物がないと通らない判定を除いた Δ）、実行順の入れ替え
- 費用の出どころの区別、Claude・Codex 別の月の予算と台帳、`doctor` の経路の変数の確認、`inherit_env`
- 同梱タスクの参照解答、実行される部品を含む配布物の警告

## [0.3.0] - 2026-10-22

### 追加

- 本物の CLI（`claude -p`・`codex exec --json`）を一時ホームで隔離して実行・採点・集計する土台、キャッシュ、レポート、LLM 判定の較正、hooks の判定、費用台帳、`harvest`

## [0.1.0] - 2026-10-21

- 最初の版（CLI を直接呼んで実行・採点する試作）
