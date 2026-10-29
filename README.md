# Agent Harness Eval

Claude Code と Codex の「ハーネス」（CLAUDE.md / AGENTS.md・Skills・Subagents・Hooks・MCP・モデル設定）を、
**同じタスクで、配布される形のまま実行して採点し、配布物なしの基準や前回の版と比べる**評価ツールです。

<img src="docs/img/overview.svg" alt="タスク・プロファイル・配布物・フィクスチャを入力に、harness-eval run が 1 試行ずつ隔離して実行・採点・集計し、report.html・summary.json・trials.jsonl を出力する" width="100%">

- 「タスク × プロファイル × 試行回数」を 1 コマンドで実行し、pass@1・pass^k・費用・失敗分類を出す
- 比較はタスク単位の 95% 区間つき。リリース判定は事前に決めたマージンで 通過／不通過／判定不能
- 各試行は一時ホームで隔離し、普段の `~/.claude` / `~/.codex` には触らない
- 採点はコード判定が基本（独自スクリプトも可）。LLM 判定は補助
- 依存は Python 3.10+ と PyYAML だけ。各 CLI（`claude -p` / `codex exec --json`）を直接呼ぶ

## ドキュメント

| 知りたいこと | 文書 |
|---|---|
| 仕組み（図解）: 使う順番・実行の流れ・隔離・採点・指標・費用・harvest・出力 | 図解ガイド — [Web](https://r1ck29.github.io/agent-harness-eval/guide.html) / [`docs/guide.html`](docs/guide.html) |
| タスク・プロファイルの書き方、グレーダー、認証、コマンドのオプション | リファレンス — [Web](https://r1ck29.github.io/agent-harness-eval/reference.html) / [`docs/reference.html`](docs/reference.html) |
| なぜこの作りか、既存ツールとの比較、確かめた CLI の挙動、残るリスク | 設計記録 — [Web](https://r1ck29.github.io/agent-harness-eval/design.html) / [`docs/design.html`](docs/design.html) |

どれも 1 ファイルの HTML で、外部と通信しません。clone や zip で手元にあるファイルをブラウザで開いても読めます。

## 導入

```bash
git clone https://github.com/R1ck29/agent-harness-eval.git
```

```bash
cd agent-harness-eval && pip install -r requirements.txt && python -m harness_eval --version
```

- 社内では zip（下の「開発」で作る）を展開し、PyYAML は Artifactory 経由で入れます。
- `pip install .` すると `harness-eval` コマンドとしても使えます（以降の `python -m harness_eval` と同じ）。
- 評価するエージェントの CLI が PATH にあること（`claude --version` / `codex --version`）。PATH にない場合はプロファイルの `executable:` で指定します。

## 5 分で試す（同梱サンプル）

結果の置き場所（`--out`）は**リポジトリとホームの外**にします。上のフォルダに `CLAUDE.md` や `.claude/` があると、
エージェントがそれを読んで比較が壊れるため、`run` が止まります（[図解ガイド 5 章](docs/guide.html#isolation)）。

```bash
OUT=/Users/Shared/harness-eval   # Windows なら C:\harness-eval など

# ① 定義の検証（費用 0）。--selftest で採点が正解例で通り、何もしなければ落ちることも確かめる
python -m harness_eval validate --tasks examples/tasks --profiles examples/profiles --selftest

# ② 環境の確認：CLI の版と環境変数（費用 0）→ 隔離した状態で認証と配布物の読み込み（最小の呼び出し 1 回ずつ）
python -m harness_eval doctor --profiles examples/profiles/claude-*.yaml --out "$OUT"
python -m harness_eval doctor --profiles examples/profiles/claude-*.yaml --out "$OUT" --live

# ③ 見積もり：呼び出し回数・キャッシュで省ける回数・概算費用（費用 0）
python -m harness_eval plan --tasks examples/tasks --profiles examples/profiles/claude-*.yaml --tag smoke --out "$OUT"

# ④ スモーク評価（1 試行・LLM 判定なし）。配布物を変えるたびにこれを回す
python -m harness_eval run --tasks examples/tasks --profiles examples/profiles/claude-*.yaml \
    --tag smoke --no-llm --baseline claude-baseline --out "$OUT"

# ⑤ フル評価（3 試行・LLM 判定あり・費用上限つき）。リリース前・モデル移行時だけ
python -m harness_eval run --tasks examples/tasks --profiles examples/profiles/claude-*.yaml \
    --trials 3 --baseline claude-baseline --max-cost-usd 5 --out "$OUT"

# ⑥ リリース判定：前回の結果と比べる（判定の決まり方と終了コードは図解ガイド 7 章）
python -m harness_eval gate --current "$OUT/<今回の実行ID>" --previous "$OUT/<前回の実行ID>" --margin 0.05
```

結果は `$OUT/<実行ID>/report.html` に出ます。自分の仕事に近いタスクで評価するなら、自分のセッション履歴から
架空化したタスクの下書きを作れます（`harvest`、[図解ガイド 9 章](docs/guide.html#harvest)）。

```bash
python -m harness_eval harvest --out my-tasks --limit 10 --ledger "$OUT/spend.jsonl"   # 件数だけ見るなら --dry-run。下書きは人が確認してから validate --selftest へ
```

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

```bash
python -m unittest discover -s tests        # 偽の claude / codex CLI を使うのでトークンは使わない
uv run pytest --cov                         # pytest・カバレッジ 80% 以上・ruff は pyproject.toml の dev グループ
mkdir -p dist && git archive --format=zip --prefix=agent-harness-eval/ -o dist/agent-harness-eval.zip HEAD -- . ':!tasks'   # 社内配布用の zip（作業メモの tasks/ は含めない）
```

`tests/fake_agents/` は実機で観測した出力形式を再現する偽 CLI、`tests/data/` は実機の出力（Claude Code の stream-json、
Codex の失敗時 JSONL、採点スクリプトの回帰用の実出力）です。

## ライセンス

MIT（[`LICENSE`](LICENSE)）。
