# 日経225オプション建玉CSV

実オプション建玉を使う場合は、このフォルダに以下の名前でCSVを置きます。

```text
data/options_oi.csv
```

CSV列:

```text
date,expiry,type,strike,open_interest
```

例:

```csv
date,expiry,type,strike,open_interest
2026-06-12,2026-06-12,C,66000,12034
2026-06-12,2026-06-12,P,64000,15320
```

`type` は Call が `C`、Put が `P` です。

このファイルが存在する場合、GitHub Actionsは暫定プロキシではなく、このCSVを使ってダッシュボードを生成します。

外部URLからCSVを取得したい場合は、GitHub Secretsに `OPTIONS_OI_URL` を設定してください。`OPTIONS_OI_URL` がある場合は、`data/options_oi.csv` よりも優先されます。

## J-Quants生CSVを置く場合

J-Quantsからダウンロードした以下のファイルもそのまま使えます。日付付きファイル名でも構いません。

```text
data/derivatives_bars_daily_options_225.csv.gz
data/indices_bars_daily_topix.csv.gz
data/derivatives_bars_daily_options_225_20260611.csv.gz
data/indices_bars_daily_topix_20260611.csv.gz
```

またはgzipなし:

```text
data/derivatives_bars_daily_options_225.csv
data/indices_bars_daily_topix.csv
data/derivatives_bars_daily_options_225_20260611.csv
data/indices_bars_daily_topix_20260611.csv
```

日経225オプションCSVは以下の列を読み取ります。

```text
Date, SQD, PCDiv, Strike, OI
```

変換ルール:

```text
date = Date
expiry = SQD
type = PCDiv 1 -> P, 2 -> C
strike = Strike
open_interest = OI
```

## J-Quants契約終了に備えたローカル退避（2026-09-19解約）

`/derivatives/bars/daily/options/225` の生データを `work/archive_jquants_options.py` でローカルに退避できます。

**重要**: 退避先はリポジトリの**外**（既定 `~/Documents/jquants_options_archive/`）に固定されています。J-Quants利用規約は「取得データを第三者が閲覧できる形で保存・配布すること」を禁じており、このリポジトリはpublicなので、`data/` 配下を含めリポジトリ内には一切コミットしません。スクリプト自体（コード）はこのリポジトリで管理しますが、生成される `.json.gz` / `manifest.csv` は対象外です。

```bash
python work/archive_jquants_options.py --years-back 10
```

`JQUANTS_API_KEY` は環境変数、`--api-key`、またはリポジトリ直下の `.env`（gitignore対象）から読みます。日付ごとに `raw/YYYY-MM-DD.json.gz`（APIレスポンスそのまま）と `manifest.csv`（date, status, record_count, fetched_at, error）を書き出し、`status` が `ok`/`no_data`/`out_of_plan_range` の日付は再実行時にスキップされる（`error` のみ再取得対象）ため、中断しても再開できます。
