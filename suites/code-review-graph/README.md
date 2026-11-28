# code-review-graph: 仕込み欠陥レビューの評価スイート

## 目的

[`R1ck29/cc-review-tool-eval`](https://github.com/R1ck29/cc-review-tool-eval) のコミット `b8372b2` にある公開可能な評価資産を、
AHE の課題形式に移植したものです（`git subtree` ではなく、選別・浄化した移植）。

- 12 個の欠陥を仕込んだ小さな Python サービス（`fixtures/repo/`、15 ファイル）と、`impact` の import 解決バグの最小再現（`fixtures/probe_importstyle/`）
- 欠陥マニフェスト（`defects.json`）と、実行で確かめた分類（`verdicts.json`）
- 決定的な採点規則（`checks/scoring.py`）と、モデルを使わない検査（`verify_defects.py`・`string_contracts.py`・`run_context_recall.py`）

将来の比較（項目 4: グラフなし / `code-review-graph` 単体 / `callers_of` 併用）では、プロファイルの `install` で
`{workdir}` を `git init && git commit` してから `code-review-graph build` を走らせる構成にします。
**この PR ではその実機比較は行いません**（モデル呼び出し 0）。

本スイートは評価用です。[`tirth8205/code-review-graph`](https://github.com/tirth8205/code-review-graph) の hooks や MCP を置き換えるものではありません。

## 構成

```
suites/code-review-graph/
  defects.json      12 欠陥のマニフェスト（クラス・変更ファイル・必要な文脈ファイル）
  verdicts.json     FIRES / LATENT / STATIC の分類
  fixtures/repo/    欠陥を仕込んだリポジトリ（全課題で共有）
  fixtures/probe_importstyle/   `from pkg import target` で impact が呼び出し元を落とす最小再現
  checks/           scoring.py・check_defect_review.py（command 採点）・verify_defects.py・string_contracts.py・run_context_recall.py
  tasks/            FIRES 9 件の課題（crg-d01 … crg-d10。D03 はない）
  references/       正解例（合成。`validate --selftest` が使う）
```

## 欠陥と課題

欠陥のクラス: **L** = 1 ファイル内 / **X** = ファイル跨ぎで静的な呼び出し・import がある / **H** = 文字列や動的ディスパッチだけで繋がる。

| 分類 | 欠陥 | 扱い |
|---|---|---|
| FIRES（実行時に壊れる）9 件 | D01, D02, D04（L）/ D05, D06, D07（X）/ D08, D09, D10（H） | 課題 `crg-dXX` にする |
| LATENT（将来の編集で壊れる結合）2 件 | D11, D12 | `verify_defects.py` だけで確認 |
| STATIC（実行では観測できない）1 件 | D03 | `verify_defects.py` だけで確認 |

各課題では、変更ファイルを示して FINDING 行形式の構造化レビューを求めます（コードは変更しない）。欠陥の内容は `defects.json` にだけ書き、プロンプトやフィクスチャには書きません。

- 結果: `check_defect_review.py --defect DXX`。最終応答（空なら作業ディレクトリの `review.md`）を `scoring.scored` で判定する
  - 照合は **1 つの FINDING の中だけ** で行う（レビュー全文にまたがった照合はしない）
  - `MATCH` の各グループから少なくとも 1 語が、その FINDING に含まれていること
  - X / H は **相手側のアンカー**（呼び出し元・スキーマ・購読側など）の名指しが必須
  - `**FINDING:**`・`- FINDING:`・`1. FINDING:` などの装飾は許容する
- 過程: コードを読んだ（`tool_used` min 1）、ファイルを変更していない（`Write|Edit|NotebookEdit|file_change` max 0）

## 検証のしかた

```bash
python suites/code-review-graph/checks/verify_defects.py          # FIRES 9 / LATENT 2 / STATIC 1、verdicts.json と一致で exit 0
python suites/code-review-graph/checks/string_contracts.py suites/code-review-graph/fixtures/repo   # H クラス 3 件・0 トークン
python -m harness_eval validate --tasks suites/code-review-graph/tasks --profiles examples/profiles --selftest
python -m pytest tests/test_crg_suite.py
```

`run_context_recall.py` は、文脈再現率とツール出力ペイロードのトークン数を決定的に測ります。実行には `code-review-graph` CLI が必要で、
対象を git リポジトリにしてグラフを build しておく必要があります。CLI の呼び出しは `run_cli()` に分けてあり、テストではスタブに差し替えます。

## 計測の履歴（移植元 `b8372b2` の要約）

### 2026-08-11（code-review-graph v2.3.7）

- 文脈再現率（FIRES 9 件）: diff-only 33% / graph 56% / graph+callers 78% / ＋文字列契約プリパス 100%。全 12 件では 33% / 67% / 83% / 100%
- 盲検レビュー（Claude CLI、9 欠陥 × 4 戦略、1 反復）の実検出率: diff-only 33% / graph 56% / graph+callers 78% / full-repo 89%
- `impact` の取りこぼし: `from pkg import module` 形式の呼び出し元が blast radius から落ちる（`fixtures/probe_importstyle/`）。`query callers_of` は名前で引くので拾える
- H クラスの FIRES 3 件は、graph では 0/3。文字列でしか繋がらない結合は、静的なコードグラフに映らない
- 採点の罠: 全文にまたがる照合、相手側アンカーの欠如、装飾行の取りこぼし、レート制限の失敗を MISS と数えること。この 4 つが結果を反転させた（`scoring.py` の規則はその対策）

### 2026-08-22（再検証）

数値はすべて再現しました（`verify_defects.py` は FIRES 9 / LATENT 2 / STATIC 1）。ただし、結論を変える限界が 3 つ見つかりました。

1. **ツール出力ペイロードがコストから抜けていた**: `impact` が返す JSON も会話に入る。ベンチでは平均 3,198 トークンで、選んだファイル（482）の約 6.6 倍。ペイロードを数えると、ベンチ上では全読みより高くなる
2. **ベンチの import 形式が偏っていた**: 取りこぼす形式はベンチでは 43.8%、追加で測った公開・非公開の実コードベースでは 0〜3.7%。偏りを補正すると graph 単体で 56% → 78% になり、`callers_of` 併用の上乗せが消える
3. **文字列契約プリパスは実リポジトリでは精度が低い**: ベンチでは 3 件すべて真陽性だが、追加で測ったコードベースでは真陽性 0（DDL が Python の文字列内にある、意図的な命名の差など）。「＋プリパスで 100%」はこのベンチ限りの値

あわせて、「文脈再現率が実検出率を完全に予測した」は採点基準（相手側の名指し）による部分的な同義だと訂正しました。言えるのは「文脈にないものは検出できない（上限）」までです。

### 2026-09-04（v2.3.7 と v2.3.8 の比較、4 ツール構成の検証）

- `uvx` が最新版を取るため、稼働していたのは未評価の 2.3.8 だった。2.3.8 では D06（X）と D11（H, LATENT）が graph で MISS になる
  - FIRES 9 件: graph 単体 56% → **44%**、graph+callers は **78% のまま**。全 12 件では graph 67% → 50%、graph+callers 83% → 75%
  - 平均ペイロードは 3,940 → 1,450 トークン。2.3.8 は絞り込みを強めた版で、`callers_of` を併用するなら改善になる。バージョンは固定して使う
- import 解決バグ（`probe_importstyle`）は 2.3.8 でも同じ挙動
- `string_contracts.py` の無言の欠落バグ 2 件（古い Python で 1 規則が消える、パースできないファイルが偽陽性を生む）は修正済み。本スイートの版は修正後のもの
- **4 ツール構成（code-review-graph・better-code-review-graph・Graphify・Serena の併用）は採用しない**: MCP の固定費が増えるだけで文脈は減らず、ソースを外部へ送りうる設定が 1 つ増える

## 移植しなかったもの

- `bench/repo_annotated_backup/`（答えをコメントに書いていた旧ベンチ。移植元で破棄済み）
- `results/*.json` の生データ（ローカルの絶対パスを含むため。`verdicts.json` だけを移植し、必要な数値は上の履歴に要約した）
- `harness/blind_review.py` の Claude CLI 実行部と盲検ランナー（AHE の runner が代わる）
