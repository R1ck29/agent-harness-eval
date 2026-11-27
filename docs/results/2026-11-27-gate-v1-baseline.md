# gate-v1 の合成基準ラン（手順 A・モデル呼び出し 0）

2026-11-28 実施 · ブランチ `feat/gate-v1-suite` · 設計: 項目 1「基準ランの具体手順」手順 A

ゲート用の代表課題スイート `suites/gate-v1/`（対象 10 + 対照 2）を、フェイクエージェントで本物の runner / preflight / 事前登録 / verdict に通した結果です。
**モデルは呼んでいません**（API 費用 $0・Codex 枠 0%・Claude 枠 0%）。確かめたのは採点・隔離・事前登録・結論の分岐であり、実モデルに対する課題の難易度ではありません（「欠測」を参照）。

## 版

| 項目 | 値 |
| --- | --- |
| AHE のベースコミット | `f8b4bcc0d1cf560950aa6fa98b2b09a4703f642a`（`origin/main`、PR #12 のスカッシュマージ）。スイートはこの上に追加した未コミットの差分で実行した（実行時点）。レポートを含むコミットの SHA は自己参照になるため記録しない |
| `harness-eval` | 0.10.3 |
| 実行環境 | Linux x86_64、Python 3.12、`bwrap` による隔離あり |
| `rules.yaml` の sha256（先頭 16 桁） | `b609d0017ffaacf5` |
| `prereg.json` の `rules_hash` | `fabc88b67a00a2f5` |
| 実行 ID | `20261128-044116-658-3f` |

### 課題のハッシュ（`summary.json` の `meta.tasks`。定義 + フィクスチャ + 採点スクリプトの内容）

| 課題 | タグ | ハッシュ |
| --- | --- | --- |
| `gate-clarify-ambiguous` | core5, clarify | `6d3773d1fe70c6b2` |
| `gate-sql-extract-edge` | core5, extraction | `20a7397c39556b7f` |
| `gate-bugfix-multi-file` | core5, coding | `372d7e11765e6c57` |
| `gate-review-cross-file` | core5, review | `e5988530789efd81` |
| `gate-refactor-preserve-api` | core5, refactor | `435b8036348cddac` |
| `gate-test-regression-add` | testing, coding | `ef921dcc33c7f1ed` |
| `gate-config-schema-migration` | data, migration | `6153e5d816b1c20a` |
| `gate-log-triage-rootcause` | debugging | `39b87c74a4330f02` |
| `gate-spec-compliance-cli` | coding, spec | `12baf93e418a2a6e` |
| `gate-safety-no-destructive` | safety | `4f68b0275a7785ad` |
| `ctrl-simple-calc` | control | `7ad2318efbdc7871` |
| `ctrl-single-file-fix` | control | `fd778f7a3db7a4e3` |

## 使ったコマンド

`OUT` は上位に `CLAUDE.md` / `AGENTS.md` / `.claude` / `.codex` / `.agents` がない、リポジトリ外の空のディレクトリ。

```bash
export TMPDIR=<書き込み可能な一時フォルダ>   # /tmp の上位には指示ファイルのあるディレクトリがあり、隔離の検査に引っかかる
OUT=<空のディレクトリ>

# 1. 定義の検証 + selftest（12/12 OK・lint 警告 0）
uv run --frozen python -m harness_eval validate --tasks suites/gate-v1/tasks --profiles examples/profiles --selftest

# 2. 静的検査とテスト全件
uv run --frozen ruff check .
uv run --frozen pytest --cov

# 3. フェイクプロファイルの生成と合成基準ラン
uv run --frozen python suites/gate-v1/checks/make_fake_profiles.py "$OUT/fake-profiles"
uv run --frozen python -m harness_eval run \
  --tasks suites/gate-v1/tasks --profiles "$OUT/fake-profiles" \
  --baseline fake-baseline --candidate fake-candidate \
  --rules suites/gate-v1/rules.yaml --control-tag control \
  --trials 2 --no-llm --seed 0 --max-cost-usd 35 \
  --out "$OUT/synthetic" --cache-dir "$OUT/cache-final"

# 4. verdict の独立再判定
RUN_ID=$(ls -1 "$OUT/synthetic" | grep '^20' | tail -n 1)
uv run --frozen python -m harness_eval verdict "$OUT/synthetic/$RUN_ID" --json > "$OUT/synthetic/$RUN_ID/verdict-check.json"
```

設計の手順 A からの差分は 2 点です。

- `--max-cost-usd 5` → **`35`**。開始前の見積もり（上限寄り: 履歴のない呼び出しを課題の `max_budget_usd` で数える）が 48 呼び出しで $30.80 になり、$5 では何も実行せずに止まる。実際の支出は $0（偽 CLI）。見積もりは課題の `max_budget_usd` から決まるため、課題側の上限は変えず、実行時の上限だけを上げた。
- 設計は `suites/code-review-graph/` の検証も並べていたが、そのスイートは PR-3 の範囲でまだ存在しないため、この PR では検証していない。

## 合成基準ラン

- フェイクエージェント `suites/gate-v1/checks/fake_gate_agent.py`（codex の `exec --json` のイベント形式だけを再現。ネットワーク・認証情報なし）。課題 ID・プロファイル ID・試行番号は作業ディレクトリの道筋から読み、解くときは正解例（`references/`）を作業ディレクトリへ重ねる。
- ベースライン `fake-baseline`: 対象 10 課題 × 2 試行のうち 12 件（60%）を解く。候補 `fake-candidate`: 18 件（90%）を解く。対照 2 課題は両方とも 100%。
- 48 試行（12 課題 × 2 試行 × 2 プロファイル）、キャッシュなし、所要約 15 秒。

### verdict

| 項目 | 結果 |
| --- | --- |
| 結論 | **IMPROVED** |
| `adoptable` | **true** |
| 終了コード（`verdict` の独立再判定） | **0** |
| ベースライン / 候補の合格率 | 60% / 90% |
| 差と 95% 区間（タスクでクラスタした 2 段ブートストラップ） | +0.30 [+0.05, +0.55] |
| 対照 | 2 課題で後退なし（差 0.0） |
| 無効な試行 | 0 / 48 |
| 事前登録 | `registered`（`rules_hash` `fabc88b67a00a2f5` が実行前の登録と一致） |
| LLM 判定 | なし（`--no-llm`。校正・判定役の系統は結論に影響しない） |

妥当性チェックは 9 件すべて `ok`（`errors` / `integrity` / `baseline_range` / `sample_size` / `controls` / `tier` / `prereg` と、`calibration`・`judge_family` の `n/a`）。

## 隔離（bwrap）の確認

- 48 試行すべてが `sandbox: bwrap`（`verdict` の `integrity`: 隔離なしの試行 0 件・答えや試行外の参照 0 件・テスト改ざん 0 件）。偽 CLI は正解例を読むため、`sandbox_ro` で `references/` だけを読み取り専用で見せた。
- `command` の採点は `bwrap` の中で、読み取り専用の作業ディレクトリに対して動いた（採点スクリプトは作業ディレクトリへ書き込まず、必要なときは `TMPDIR` の一時フォルダへ複製する）。`decisive` 段階が求める「採点の隔離が確認できること」を満たしている。
- 既存の隔離テスト（`tests/test_cheatproof.py` の Sandbox ほか）はテスト全件の実行に含まれる。結果は次節。

## 検査の結果

| 検査 | 結果 |
| --- | --- |
| `validate --tasks suites/gate-v1/tasks --profiles examples/profiles --selftest` | 12 課題 OK・selftest 12/12・lint 警告 0 |
| `ruff check .` | 通過 |
| `pytest --cov` | 633 passed・1 skipped（実 CLI を使う既存のオプトインテスト）・所要 4 分 14 秒。カバレッジ 92.21%（下限 80%） |
| `tests/test_gate_v1_suite.py`（新規） | 25 件通過・約 4 秒（12 課題の selftest が約 2 秒、ほかは各 0.3 秒以下） |

## この作業で見つかったこと

- **`cheatproof` の `skip_added` の偽陽性（本体は未修正）**: `SKIP_ADD` の `xit\(`（jest の skip）が `sys.exit(` / `exit(` の部分一致に反応し、新しく書いた Python ファイルが「見えているテストの改ざん」と判定されて不合格になる。最初の合成ランで、CLI を書く 2 課題（`gate-config-schema-migration`・`gate-spec-compliance-cli`）の正しい解が、候補の試行を含めて 0 点になって発覚した。本スコープでは本体を変えず、**見えているテストを持たない 5 課題**に `allow_test_changes: true` を付けて回避し、`suites/gate-v1/README.md` の「既知の制限」に載せた（見えているテストを持つ課題 3・5・6・12 では改ざん検出を残している）。本体の修正（`xit\(` に単語境界を付けるなど）は別 PR で扱うのがよい。
- **`.gitignore` の `build/`**: 安全課題のフィクスチャのディレクトリ名を `build/` にするとコミットされないため、`artifacts/` にした。
- **`ruff`**: スイートの `fixtures/` と `references/` は意図的に不具合・未使用 import を含む合成コードなので、`pyproject.toml` の `[tool.ruff] extend-exclude` で検査対象から外した（設定のみの変更）。

## 欠測（この結果から言えないこと）

- **手順 B（実機の Codex 疎通・難易度スポットチェック）は実施していない**。実装時点で Codex の週間使用量が 70% 以上だったため、設計の方針（残量が 70% を超えるなら実機の難易度確認は見送る）に従って見送った。したがって、各課題の実モデルに対する難易度（床 20%・天井 80% に収まるか）と、実 CLI（Codex 0.172.0 など）での `bwrap` の疎通は**未確認**。合成ランの「ベースライン 60%・候補 90%」は偽エージェントの設定値であり、実モデルの実力ではない。
- API 課金を伴う実行はなく、実モデルのトランスクリプトもない（このレポートに生のトランスクリプト・認証情報・端末のホームのパスは含めていない）。
- LLM 判定（`tasks-llm/`）と校正の**実測**はしていない。校正ゲートの受理・拒否は `tests/test_gate_v1_suite.py` が記録を組んで確かめている。校正用の `calibration:` は課題 1〜6 に置いたが、判定役が 1 ファイルしか見られない課題 3・5・6 の一致率は実測で確かめる必要がある。
- Windows / macOS では `bwrap` がなく、採点の隔離は「なし」と記録され、`decisive` の結論は保留される（設計どおり。採用判定は Linux + `bwrap` で行う）。
