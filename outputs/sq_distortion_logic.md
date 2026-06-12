# 日経225 SQ歪みモデル ロジック仕様 v0.1

作成日: 2026-06-11

## 目的

日経平均225のメジャーSQ前後に発生しやすい「指数の歪み」を、日次で観測・スコア化し、上方向または下方向の勝率が通常より偏る局面を抽出する。

このモデルは価格水準そのものを予測するものではなく、以下を検出する。

- 高寄与度銘柄だけで指数が押し上げられている局面
- オプション建玉が厚い行使価格へ指数が吸い寄せられている局面
- SQ通過後に需給が剥落しやすい局面
- TOPIXや騰落銘柄数との乖離が大きい局面

## 中核仮説

日経225は価格加重型指数であるため、少数の高寄与度銘柄が動くだけで指数全体が大きく動く。SQ前には先物・オプションのヘッジ、裁定、ロール、建玉解消が重なるため、この構造的な偏りが増幅される。

特に観測対象とする銘柄群:

- 285A キオクシア
- 5803 フジクラ
- 6857 アドバンテスト
- 9984 ソフトバンクグループ
- 8035 東京エレクトロン
- 9983 ファーストリテイリング
- 6920 レーザーテック
- その他、日次寄与度上位銘柄

## 基本式

日経平均:

```text
N_t = Σ(a_i * P_i,t) / D_t
```

銘柄 i の日次指数寄与度:

```text
Contribution_i,t = a_i * (P_i,t - P_i,t-1) / D_t
```

寄与度上位銘柄の合計:

```text
TopContribution_t = Σ Contribution_i,t  for i in TopK
```

上位銘柄への集中度:

```text
ContributionConcentration_t =
  abs(Σ Contribution_i,t for TopK) / Σ abs(Contribution_i,t for all constituents)
```

## 指標セット

### 1. 寄与度トレンドスコア

高寄与度銘柄の短期モメンタムを指数ウェイトで合成する。

```text
Momentum_i,t = close_i,t / close_i,t-n - 1

ContributionTrend_t =
  Σ weight_i,t * Momentum_i,t
```

推奨 n:

- 3営業日
- 5営業日
- 10営業日

解釈:

- プラス大: 高寄与度銘柄が指数を上に牽引
- マイナス大: 高寄与度銘柄が指数を下に牽引

### 2. SQストライク吸引スコア

指数現在値が、日経225オプションの建玉集中ストライクに近づいているかを見る。

```text
Distance_K,t = abs(N_t - K) / ATR_N,t

StrikeMagnet_t =
  Σ OI_K,T * exp(-Distance_K,t)
```

方向付きスコア:

```text
CallMagnet_t = Σ CallOI_K,T * exp(-Distance_K,t) for K >= N_t
PutMagnet_t  = Σ PutOI_K,T  * exp(-Distance_K,t) for K <= N_t

OptionBias_t = z(CallMagnet_t - PutMagnet_t)
```

解釈:

- `OptionBias > 0`: 上方向の建玉ゾーンが近い
- `OptionBias < 0`: 下方向の建玉ゾーンが近い
- 絶対値が大きいほど、SQに絡む需給の存在感が強い

### 3. SQ時間圧力スコア

SQ日が近いほど、建玉・ヘッジ・ロールの影響が強まりやすい。

```text
DaysToSQ_t = 次回SQ日までの営業日数

TimePressure_t =
  max(0, 1 - DaysToSQ_t / 5)
```

解釈:

- SQ 5営業日前: 0
- SQ 3営業日前: 0.4
- SQ前日: 0.8
- SQ当日: 1.0

### 4. 指数歪みスコア

日経平均だけが強く、TOPIXや市場の広がりが追随していない局面を検出する。

```text
NikkeiTopixGap_t = return_Nikkei_t - return_TOPIX_t

BreadthDivergence_t =
  return_Nikkei_t - z(advancers_ratio_t)

IndexDistortion_t =
  z(NikkeiTopixGap_t)
  + z(ContributionConcentration_t)
  - z(advancers_ratio_t)
```

解釈:

- プラス大: 少数銘柄主導の指数上昇
- マイナス大: 指数より市場全体が強い、または下落が広がっている

### 5. 先物ベーシススコア

先物が現物指数より強いかを見る。

```text
Basis_t = Futures_t - N_t

BasisScore_t = z(Basis_t / ATR_N,t)
```

解釈:

- プラス大: 先物主導の買い圧力
- マイナス大: 先物主導の売り圧力

## 合成スコア

上方向SQ歪みスコア:

```text
SQ_UpPressure_t =
  0.30 * z(ContributionTrend_t)
  + 0.25 * z(OptionBias_t)
  + 0.20 * z(BasisScore_t)
  + 0.15 * z(IndexDistortion_t)
  + 0.10 * TimePressure_t
```

下方向SQ剥落スコア:

```text
SQ_DownRisk_t =
  0.25 * z(-ContributionTrend_t)
  + 0.20 * z(-OptionBias_t)
  + 0.20 * z(-BasisScore_t)
  + 0.20 * z(IndexDistortion_t)
  + 0.15 * PostSQFlag_t
```

ここで:

```text
PostSQFlag_t = 1 if SQ通過後3営業日以内 else 0
```

## 勝率見立て

売買判断ではなく、確率的なレジーム判定として扱う。

```text
P_Up_t = logistic(
  b0
  + b1 * SQ_UpPressure_t
  - b2 * SQ_DownRisk_t
)
```

初期値の目安:

```text
P_Up_t = logistic(-0.05 + 0.85 * SQ_UpPressure_t - 0.65 * SQ_DownRisk_t)
```

判定:

```text
P_Up >= 0.60: 上方向優位
P_Up <= 0.40: 下方向優位
0.40 < P_Up < 0.60: 中立
```

## シグナル条件

### 上方向優位

```text
SQ_UpPressure_t >= 1.0
and TimePressure_t >= 0.4
and ContributionTrend_t > 0
and BasisScore_t >= 0
```

補強条件:

```text
NikkeiTopixGap_t > 0
and ContributionConcentration_t above 60th percentile
and 現値上方に厚いCall OIがある
```

### 下方向剥落リスク

```text
SQ_DownRisk_t >= 1.0
and (
  PostSQFlag_t = 1
  or ContributionTrend_t < 0
  or BasisScore_t < 0
)
```

補強条件:

```text
SQ前に指数だけが上昇
and TOPIXが追随していない
and 上位寄与銘柄の上昇が失速
```

## 推奨グラフ

### 1. SQ歪みメーター

表示項目:

- SQ_UpPressure
- SQ_DownRisk
- P_Up
- 判定ラベル

目的:

日次の状態を一目で把握する。

### 2. 寄与度上位銘柄バー

表示項目:

- 当日寄与度トップ10
- 5日累積寄与度トップ10

目的:

指数の上昇・下落がどの銘柄に偏っているかを確認する。

### 3. オプション建玉ヒートマップ

表示項目:

- 行使価格
- Call OI
- Put OI
- 現在の日経平均位置
- SQまでの日数

目的:

指数がどのストライクへ吸い寄せられやすいかを見る。

### 4. 日経平均 vs TOPIX 乖離

表示項目:

- 日経平均リターン
- TOPIXリターン
- 差分

目的:

指数だけが膨らんでいるかを見る。

### 5. SQカレンダー・イベント帯

表示項目:

- メジャーSQ日
- SQ前5営業日
- SQ後3営業日

目的:

時系列グラフ上で需給イベントの窓を明示する。

## 必要データ

必須:

- 日経225構成銘柄の日次OHLCV
- 日経225構成銘柄の調整係数
- 日経225除数
- 日経平均日次OHLC
- TOPIX日次OHLC
- 日経225先物価格
- 日経225オプション建玉、行使価格、限月、Call/Put
- SQカレンダー

推奨:

- 騰落銘柄数
- 売買代金
- 先物出来高
- オプション出来高
- IV
- 海外要因: Nasdaq, SOX, USDJPY, 米金利

## 検証方法

### イベントスタディ

対象:

- メジャーSQ前5営業日
- SQ当日
- SQ後3営業日

評価:

- 翌日方向正解率
- SQ前3営業日累積リターンの方向正解率
- SQ後3営業日反落確率
- 平均リターン
- Sharpe-like score
- 最大逆行幅

### 比較ベースライン

- 常に買い
- 前日リターン順張り
- 5日移動平均順張り
- 日経平均 vs TOPIX 単純乖離

## 初期実装方針

1. 日次データを1行1日に集約する
2. 銘柄別寄与度を計算する
3. オプション建玉を行使価格別に集計する
4. SQまでの営業日数を付与する
5. 各スコアをz-score化する
6. 合成スコアを計算する
7. 判定ラベルとグラフ用データを出力する

## 注意点

- 建玉だけでは投資家の売買方向は分からない
- ディーラーのネットガンマは推定になる
- 日経225構成銘柄や調整係数は定期的に変わる
- SQ当日は寄付き特殊需給が大きいため、終値ベースだけでは不十分な場合がある
- キオクシアのような新規採用・大型化銘柄は、過去データが短い点に注意する

## v0.2で追加したい要素

- Gamma Exposure風の近似指標
- ストライク別 Max Pain 近似
- 半導体バスケットとの連動スコア
- SQ前後だけを対象にしたロジスティック回帰
- データ欠損時のフォールバックロジック
