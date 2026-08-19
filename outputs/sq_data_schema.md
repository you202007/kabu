# SQ歪みモデル 入力CSVスキーマ

実装ファイル:

```text
work/sq_distortion_model.py
```

## 実行例

サンプルデータ生成込み:

```powershell
& 'C:\Users\eveap\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' work\sq_distortion_model.py --generate-sample
```

実データを `work/input_data` に置いて実行:

```powershell
& 'C:\Users\eveap\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' work\sq_distortion_model.py --input-dir work\input_data --output-dir outputs
```

## constituents_daily.csv

日経225構成銘柄の日次データ。

| column | type | description |
|---|---:|---|
| date | date | 取引日 |
| ticker | string | 銘柄コード |
| name | string | 銘柄名 |
| close | number | 終値 |
| prev_close | number | 前営業日終値 |
| adj_factor | number | 日経平均の調整係数 |
| volume | number | 出来高 |

## index_daily.csv

指数・先物・市場幅の日次データ。

| column | type | description |
|---|---:|---|
| date | date | 取引日 |
| nikkei_close | number | 日経平均終値 |
| nikkei_prev_close | number | 日経平均前日終値 |
| topix_close | number | TOPIX終値 |
| topix_prev_close | number | TOPIX前日終値 |
| nikkei_atr | number | 日経平均ATR。なければ暫定で20日平均値幅など |
| futures_close | number | 日経225先物終値 |
| advancers_ratio | number | 値上がり銘柄比率。0から1 |

## options_oi.csv

日経225オプションの行使価格別建玉。

| column | type | description |
|---|---:|---|
| date | date | 取引日 |
| expiry | date | 限月SQ日 |
| type | string | C または P |
| strike | number | 行使価格 |
| open_interest | number | 建玉 |

## sq_calendar.csv

SQカレンダー。

| column | type | description |
|---|---:|---|
| sq_date | date | SQ日 |
| kind | string | major または minor |

## 出力

| file | description |
|---|---|
| sq_scores.csv | 日次スコア、勝率、判定 |
| sq_contributions.csv | 銘柄別寄与度、順位、モメンタム |
| sq_latest_signal.json | 最新日の判定サマリー |
| sq_distortion_dashboard_generated.html | 自動生成ダッシュボード（`sq.html`の元データ） |
| regime_transition.json | cockpit.html第1層のレジーム転換6条件パネル用（`work/regime_transition.py`が生成） |

## v3で追加した設定・永続化ファイル

| file | description |
|---|---|
| work/config/sector_baskets.json | 改修2のセクターバスケット定義（現在は`semiconductor`のみ）。銘柄リストをコードから外出し |
| data/regime_manual_inputs.json | 改修1の手動入力条件（2. 企業利益予想の下方修正、4. AI capexの後退）。人が編集してコミットする。CIは読むだけで書き換えない |
| data/regime_transition_state.json | 改修1の自動取得4条件（1,3,5,6）のlast-known-goodアーカイブ。CIが毎回書き換える。手動入力は混ざらない |
| data/sq_score_history.csv | 改修3のスコア履歴アーカイブ。append-only・immutable（既存日付の行は再実行しても上書きされない）。`zscore_window`・`basis_included`列でロジック世代を区別できる |

これらのうち `data/*` はCIが `[skip ci]` 付きコミットで書き戻す（`.github/workflows/pages.yml`）。`data/regime_manual_inputs.json` だけは例外で、常に人が手で編集する。
