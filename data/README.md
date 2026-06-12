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

J-Quantsからダウンロードした以下のファイルもそのまま使えます。

```text
data/derivatives_bars_daily_options_225.csv.gz
data/indices_bars_daily_topix.csv.gz
```

またはgzipなし:

```text
data/derivatives_bars_daily_options_225.csv
data/indices_bars_daily_topix.csv
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
