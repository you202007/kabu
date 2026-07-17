# Nikkei 225 SQ Distortion Dashboard

日経225のSQ前後に発生しやすい指数の歪みを、寄与度、オプション建玉、先物ベーシス、TOPIX乖離から日次でスコア化する試作プロジェクトです。

## Outputs

- `outputs/sq_scores.csv`: 日次スコア
- `outputs/sq_contributions.csv`: 銘柄別寄与度
- `outputs/sq_latest_signal.json`: 最新日の判定
- `outputs/sq_distortion_dashboard_generated.html`: 自動生成ダッシュボード

GitHub Pagesでは `outputs/index.html` を公開します。

## Local Run

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

## Market Data Run

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

`.github/workflows/pages.yml` により、`main` ブランチへpushされるたびに以下を実行します。

1. Python依存関係をインストール
2. Yahoo Financeから株価・指数を取得
3. オプション建玉CSVがない場合は暫定プロキシを生成
4. `outputs/sq_distortion_dashboard_generated.html` を `outputs/index.html` にコピー
5. `cockpit/` 配下のファイルを `outputs/cockpit/` にコピー
6. `outputs` ディレクトリをGitHub Pagesへデプロイ

実際のオプション建玉を接続する場合は、WorkflowにCSV取得処理を追加し、`--allow-proxy-options` を外します。

## 関連ツール

- [`cockpit/`](cockpit/): マクロ・フロー統合コックピット（レジーム判定＋SQ戦術＋フロー警報を束ねた判断補助ツール）。公開URL: `https://youri202007.github.io/kabu/cockpit/`
