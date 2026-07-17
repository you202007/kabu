# kabu — マクロ・フロー統合システム

日本株・円・マクロを **3つの時間軸レイヤー** で束ねて、
「今どのレジームか → どの傾きが有利か → いつ・どう執行するか → どのフローが近づいているか」
を一望するための判断補助システムです。GitHub Pages で3ページを配信します。

> ⚠️ 本システムは自分の観測を整理するための**判断補助**であり、投資助言・売買指示ではありません。
> 数値・傾き・リード期間はすべて目安です。

## ページ構成

- [`index.html`](index.html) … ランディングページ（下記2ページへの入り口、二枚の使い方を説明）
- [`cockpit.html`](cockpit.html) … マクロ・フロー・コックピット（第1層レジーム＋第3層フロー警報＋SQ戦術パネル。手動入力のブラウザ完結ツール、静的ファイル）
- `sq.html` … 日経225 SQ歪みダッシュボード（`work/sq_distortion_model.py` / `work/fetch_market_data.py` が yfinance＋J-Quants実データから自動生成。GitHub Actionsが `main` へのpushと平日20:00 JSTに再生成）

公開URL: `https://youri202007.github.io/kabu/`（ランディング） / `https://youri202007.github.io/kabu/cockpit.html` / `https://youri202007.github.io/kabu/sq.html`

## コックピット（判断レイヤー）

| レイヤー | 時間軸 | 役割 | 中身 |
|---|---|---|---|
| 第1層 レジーム | 構造・週次 | スタンスを決める | 9KPI（円の避難挙動／USDJPY／米実質金利／Fed／JGB vs 3%／原油・地政学／AI・capex／資金ストレス／流動性）→ 4レジーム分類＋資産の傾き |
| 第2層 SQ戦術 | 需給・SQ週 | 執行モードを決める | SQ距離／D-1出来高／Max Pain／Pinning／CME夜間 → **レジーム×SQ** で執行モードが反転（ピン順張り／ブレイク追随／見送り） |
| 第3層 フロー警報 | カレンダー | 接近イベントを警戒 | 四半期末リバランス・配当再投資・指数リバランス（FTSE/Russell・MSCI・日経225）・SQ・決算/自社株買いブラックアウト等を自動計算し、接近アラート＋直近通過を表示 |

**設計の肝**：第1層のレジームが、第2層のSQ信号と第3層のイベントの「読み方」を上から書き換える。
同じ D-1 出来高スパイクでも、リスクオンなら「収束取り（ピン順張り）」、抑圧・危機なら「回避／ブレイク追随」に反転する。

`cockpit.html` は SQダッシュボード（`sq.html`）で読んだ実数値（D-1出来高の異常度、Max Painとの位置、Pinning状態）をSQ戦術パネルに手入力して使う。データは下から（`sq.html`）、判断は上から（`cockpit.html`）。

「この週を記録」でスナップショットを保存（ブラウザのストレージに保持。レジーム遷移を週次で追える）。

## SQダッシュボード（データレイヤー）

日経225のSQ前後に発生しやすい指数の歪みを、寄与度、オプション建玉、先物ベーシス、TOPIX乖離から日次でスコア化する試作プロジェクトです。

### Outputs

- `outputs/sq_scores.csv`: 日次スコア
- `outputs/sq_contributions.csv`: 銘柄別寄与度
- `outputs/sq_latest_signal.json`: 最新日の判定
- `outputs/sq_distortion_dashboard_generated.html`: 自動生成ダッシュボード（`sq.html` の元データ）

### Local Run

```powershell
& 'C:\Users\eveap\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' work\sq_distortion_model.py --generate-sample
```

一般的なPython環境では以下で実行できます。

```bash
pip install -r requirements.txt
python work/sq_distortion_model.py --generate-sample
```

実データを使う場合は、`work/input_data` にCSVを置いて実行します。

```bash
python work/sq_distortion_model.py --input-dir work/input_data --output-dir outputs
```

入力CSVの仕様は `outputs/sq_data_schema.md` を参照してください。

### Market Data Run

株価・指数をYahoo Financeから取得し、オプション建玉は暫定プロキシで生成します。

```bash
python work/fetch_market_data.py --allow-proxy-options --period 3mo
```

実際の日経225オプション建玉CSVがある場合:

```bash
python work/fetch_market_data.py --options-csv path/to/options_oi.csv --period 3mo
```

GitHub Actionsでは、`data/options_oi.csv` が存在する場合に自動で実建玉CSVとして使用します。

J-Quantsからダウンロードした生CSVを使う場合は、以下の名前で `data/` に置くと自動変換されます。

```text
data/derivatives_bars_daily_options_225.csv.gz
data/indices_bars_daily_topix.csv.gz
```

日付付きのままでも使えます。

```text
data/derivatives_bars_daily_options_225_20260611.csv.gz
data/indices_bars_daily_topix_20260611.csv.gz
```

HTTP(S)で取得できるCSVがある場合:

```bash
python work/fetch_market_data.py --options-url https://example.com/options_oi.csv --period 3mo
```

注意: `--allow-proxy-options` はダッシュボード更新用の暫定値です。売買判断や勝率検証には、JPX/OSE由来の実建玉データを `options_oi.csv` として接続してください。

J-QuantsのAPIキーがある場合:

```bash
python work/fetch_market_data.py --jquants-api-key <api-key> --period 3mo
```

GitHub Actionsでは、Repository Secretsに `JQUANTS_API_KEY` を設定すると、J-Quantsの日経225オプション四本値APIを優先して使用します。既存の `JQUANTS_REFRESH_TOKEN` も互換的に読みます。

日次更新はGitHub Actionsで平日20:00 JSTに実行されます。

## GitHub Pages

`.github/workflows/pages.yml` により、`main` ブランチへpushされるたび（および平日20:00 JSTの定期実行）に以下を実行します。

1. Python依存関係をインストール
2. Yahoo Financeから株価・指数を取得
3. オプション建玉CSVがない場合は暫定プロキシを生成
4. `outputs/sq_distortion_dashboard_generated.html` を `outputs/sq.html` にコピー
5. ルートの `index.html`・`cockpit.html`（静的ファイル）を `outputs/` にコピー
6. `outputs` ディレクトリをGitHub Pagesへデプロイ（`index.html`・`cockpit.html`・`sq.html` の3ページ構成で公開）

実際のオプション建玉を接続する場合は、WorkflowにCSV取得処理を追加し、`--allow-proxy-options` を外します。

## TODO / 拡張候補

- 祝日リスト（`jpholiday` 相当）を組み込み、営業日判定を厳密化
- イベント項目の拡充（先物限月交代・TOPIX浮動株見直し・大型ETFの分配金捻出売り 等）
- SQダッシュボードとコックピットSQ戦術パネルの連携（現状は手入力）

---
*Generated with Claude. 判断補助ツールであり投資助言ではありません。*
