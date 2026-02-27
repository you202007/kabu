import pandas as pd
import numpy as np
import os
from datetime import datetime
from tqdm import tqdm
from scipy import stats

# ==========================================
# 1. 統合コントローラー・設定パラメータ
# ==========================================
RUN_MODE = 'daily'  # 'daily': 今日の予測を生成 / 'evaluate': 過去の予測の答え合わせ / 'portfolio': ポートフォリオだけ生成

# --- Evaluate（検証）モード用の設定 ---
EVAL_TARGET_DATE = datetime.now() - pd.Timedelta(days=7)  # 検証したい「過去の実行日」(YYYYMMDD形式で指定)
FORWARD_DAYS = 5               # 評価期間 (3 または 5 営業日後)

# --- 投資家設定・パス ---
CAPITAL = 1000000               # 総資金 100万円
RISK_PER_TRADE = 20000          # 1トレード許容損失 (2%)
LIQUIDITY_THRESHOLD = 100000000 # 流動性フィルター (1億円)

DATA_PATH = '/content/drive/MyDrive/JQuantsData/data/master.parquet'
PREDICT_DIR = '/content/drive/MyDrive/JQuantsData/predict/'
OUTPUT_S1 = PREDICT_DIR + 'strategy_1_spear_ride.csv'
OUTPUT_S2 = PREDICT_DIR + 'strategy_2_persistence.csv'

# 履歴（スナップショット）保存先
HISTORY_DIR = PREDICT_DIR + 'history/'

# --- daily実行後にポートフォリオも自動生成する ---
RUN_PORTFOLIO_AFTER_DAILY = True

# --- Portfolio Builder用 ---
MASTER_2026_PATH = '/content/drive/MyDrive/JQuantsData/data/master_2026.csv'  # 2026年の株価データ（CSV）
MAX_POSITIONS = 5
TOTAL_RISK_CAP = 100000          # 全銘柄がSLになった時の損失上限（例：10万円=資金の10%）
EV_BLEND_ALPHA = 0.5             # 0.0=モデルEVのみ, 1.0=実測のみ（実測が十分あるときのみ寄せる）
EMP_MIN_N = 30                   # 実測サンプルがこの件数未満なら実測を無効化（モデル寄り）
PORTFOLIO_OUT_PREFIX = 'portfolio_'


# ==========================================
# 2. IQ1000 解析エンジン (改良統合版)
# ==========================================
class OmnisEngineV5_2:
    def __init__(self, epsilon=1e-10):
        self.epsilon = epsilon

    # --- True Range (ATRの素材) ---
    def get_atr(self, high, low, close):
        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]))
        )
        return tr

    # --- 既存の連続スコア（トレンド×収縮×短期モメ） ---
    def get_continuous_score(self, df):
        close = df['AdjC'].astype(float).values
        high = df['AdjH'].astype(float).values
        low = df['AdjL'].astype(float).values

        if len(close) < 25:
            return np.nan, np.nan, np.nan

        tr = self.get_atr(high, low, close)
        if len(tr) < 20 or np.all(np.isnan(tr[-20:])):
            return np.nan, np.nan, np.nan

        atr_5 = np.nanmean(tr[-5:])
        atr_20 = np.nanmean(tr[-20:])

        if np.isnan(atr_5) or np.isnan(atr_20) or (atr_20 + self.epsilon) == 0:
            return np.nan, np.nan, np.nan

        vcp_ratio = atr_5 / (atr_20 + self.epsilon)     # 小さいほど収縮
        s_vcp = 1.0 - np.clip(vcp_ratio, 0, 1)          # 小さいほど高得点

        ma25 = np.nanmean(close[-25:])
        if np.isnan(ma25) or (ma25 + self.epsilon) == 0:
            return np.nan, np.nan, np.nan

        trend_gap = (close[-1] - ma25) / (ma25 + self.epsilon)
        s_trend = np.tanh(trend_gap * 10)

        if len(close) < 5 or np.isnan(close[-1]) or np.isnan(close[-5]) or (close[-5] + self.epsilon) == 0:
            return np.nan, np.nan, np.nan

        s_mom = np.tanh((close[-1] - close[-5]) / (close[-5] + self.epsilon) * 5)

        if np.isnan(s_trend) or np.isnan(s_vcp) or np.isnan(s_mom):
            return np.nan, np.nan, np.nan

        final_score = (s_trend * 0.4) + (s_vcp * 0.4) + (s_mom * 0.2)
        return final_score, trend_gap, vcp_ratio

    # --- Kelly（保持：今回は株数計算には使わない。出力や将来拡張用） ---
    def kelly_lot_size(self, win_prob, rr_ratio):
        p = win_prob
        q = 1 - p
        b = rr_ratio
        kelly_f = p - (q / b)
        return np.clip(kelly_f * 0.5, 0, 0.1)

    # -------------------------
    # 資金管理：固定リスク法（王道）
    # -------------------------
    def risk_based_shares(self, code: str, risk_per_share: float):
        """
        1トレード許容損失 RISK_PER_TRADE を基準に株数決定。
        日本株の単元(100株)調整もここで実施。
        """
        if np.isnan(risk_per_share) or risk_per_share <= 0:
            return 0

        shares = int(RISK_PER_TRADE // risk_per_share)
        if shares <= 0:
            return 0

        # ETF等の例外（あなたの既存ルール踏襲）
        if str(code).startswith('1308'):
            return shares

        return (shares // 100) * 100

    # -------------------------
    # 爆発の起爆剤：高値更新 + 出来高(代金)急増
    # -------------------------
    def breakout_volume_features(self, df, lookback=20):
        """
        価格ブレイクアウト：当日終値が過去lookback日(当日除く)の高値を更新
        出来高(代金)急増：当日Va / 過去平均Va
        """
        close = df['AdjC'].astype(float).values
        va = df['Va'].astype(float).values

        if len(close) < lookback + 2:
            return None, None

        prev_high = np.nanmax(close[-(lookback + 1):-1])  # 当日を除く過去高値
        breakout = 1.0 if close[-1] > prev_high else 0.0

        va_base = np.nanmean(va[-(lookback + 1):-1])
        if np.isnan(va_base) or va_base <= 0:
            vol_ratio = np.nan
        else:
            vol_ratio = va[-1] / va_base

        return breakout, vol_ratio

    # ==========================================
    # Strategy 1: 短期（爆発初動＋長期移行）
    # ==========================================
    def analyze_strategy_1(self, df):
        if len(df) < 25:
            return None

        va = df['Va'].astype(float).values
        avg_va_20 = np.nanmean(va[-20:])
        if np.isnan(avg_va_20) or avg_va_20 < LIQUIDITY_THRESHOLD:
            return None

        base_score, trend_gap, vcp_ratio = self.get_continuous_score(df)
        if np.isnan(base_score):
            return None

        close = float(df['AdjC'].iloc[-1])
        if np.isnan(close):
            return None

        tr = self.get_atr(df['AdjH'].values, df['AdjL'].values, df['AdjC'].values)
        atr_now = np.nanmean(tr[-20:])
        if np.isnan(atr_now) or atr_now <= 0:
            return None

        # SL/TP（従来の枠組み踏襲）
        stop_loss = close - (atr_now * 2.0)
        risk_per_share = close - stop_loss
        if np.isnan(risk_per_share) or risk_per_share <= 0:
            return None

        take_profit = close + (risk_per_share * 3.0)

        code = str(df['Code'].iloc[0])

        # 起爆剤（ブレイクアウト + 出来高(代金)倍率）
        breakout, vol_ratio = self.breakout_volume_features(df, lookback=20)
        if breakout is None:
            return None

        vol_boost = 0.0 if np.isnan(vol_ratio) else np.tanh((vol_ratio - 1.0) * 0.8)  # 1倍超でプラス
        s_break = breakout  # 0 or 1

        # スコア統合：爆発に寄せて起爆要素を加点
        final_score = (base_score * 0.75) + (s_break * 0.15) + (vol_boost * 0.10)

        # 株数：固定リスク法（RISK_PER_TRADEを実際に使う）
        shares = self.risk_based_shares(code, risk_per_share)
        if shares <= 0:
            return None

        # 資金制約ガード
        need_cash = shares * close
        if need_cash > CAPITAL:
            shares = int(CAPITAL // close)
            shares = shares if code.startswith('1308') else (shares // 100) * 100
            if shares <= 0:
                return None
            need_cash = shares * close

        # フェーズ判定：vcp_ratioは“小さいほど収縮”なので矛盾を解消
        if (trend_gap > 0.05) and (vcp_ratio < 0.8):
            phase = "Ride (長期保有移行可)"
        elif (vcp_ratio < 0.6) and (breakout > 0):
            phase = "Spear (爆発初動)"
        else:
            phase = "Spear (初動候補)"

        # 文章生成
        raw_days = (take_profit - close) / atr_now
        est_days = int(np.clip(raw_days, 3, 30))
        hold_period = "1ヶ月以上" if est_days >= 30 else f"約{est_days}営業日"

        reasons = []
        reasons.append(f"VCP比:{vcp_ratio:.2f}")
        reasons.append(f"MA乖離:{trend_gap * 100:.1f}%")
        if breakout > 0:
            reasons.append("20日高値更新(ブレイク)")
        if not np.isnan(vol_ratio) and vol_ratio >= 1.5:
            reasons.append(f"代金急増:{vol_ratio:.2f}x")
        reason_text = " / ".join(reasons)

        fail_cond = f"株価が損切ライン({stop_loss:.1f}円)を下回り、初動シナリオが否定された場合"

        # 勝率は出力用（Kellyで資金配分しない）
        win_prob = np.clip(0.45 + (final_score * 0.2), 0.1, 0.8)

        return {
            'コード': code,
            'スコア': round(float(final_score), 4),
            'フェーズ': phase,
            '勝率': round(float(win_prob), 2),
            '推奨株数': int(shares),
            '現在値': close,
            '必要資金': int(need_cash),
            '損切(SL)': round(float(stop_loss), 1),
            '利確(TP)': round(float(take_profit), 1),
            '選定理由': reason_text,
            '推奨保有期間': hold_period,
            '破綻条件': fail_cond
        }

    # ==========================================
    # Strategy 2: 長期（持続性）
    # ==========================================
    def analyze_strategy_2(self, df):
        # 60→120にして“長期”らしく
        if len(df) < 120:
            return None

        close_raw = df['AdjC'].astype(float).values
        high = df['AdjH'].astype(float).values
        low = df['AdjL'].astype(float).values

        close_log = np.log(close_raw[-120:])
        x = np.arange(len(close_log))

        slope, intercept, r_value, p_value, std_err = stats.linregress(x, close_log)
        r_squared = r_value ** 2

        if std_err is None or np.isnan(std_err) or std_err == 0:
            return None
        t_stat = slope / std_err

        # 上昇トレンド + 直線性 + 有意性（偽陽性を落とす）
        if np.isnan(slope) or slope <= 0:
            return None
        if np.isnan(r_squared) or r_squared < 0.4:
            return None
        if np.isnan(t_stat) or t_stat < 2.0:
            return None

        persistence_score = r_squared * slope

        tr = self.get_atr(high, low, close_raw)
        atr_now = np.nanmean(tr[-20:])
        if np.isnan(atr_now) or atr_now <= 0:
            return None

        curr_p = float(close_raw[-1])
        stop_loss = curr_p - (atr_now * 3.0)
        risk_per_share = curr_p - stop_loss
        if np.isnan(risk_per_share) or risk_per_share <= 0:
            return None

        take_profit = curr_p + (risk_per_share * 2.0)

        code = str(df['Code'].iloc[0])

        # 株数：固定リスク法に統一
        shares = self.risk_based_shares(code, risk_per_share)
        if shares <= 0:
            return None

        need_cash = shares * curr_p
        if need_cash > CAPITAL:
            shares = int(CAPITAL // curr_p)
            shares = shares if code.startswith('1308') else (shares // 100) * 100
            if shares <= 0:
                return None
            need_cash = shares * curr_p

        raw_days = (take_profit - curr_p) / atr_now
        est_days = int(np.clip(raw_days, 3, 30))
        hold_period = "1ヶ月以上" if est_days >= 30 else f"約{est_days}営業日"

        reason_text = f"ログ回帰: R2={r_squared:.2f}, t={t_stat:.2f} による安定上昇トレンド"
        fail_cond = f"株価が損切ライン({stop_loss:.1f}円)を下回り、長期トレンドが崩壊した場合"

        win_prob = np.clip(0.45 + (r_squared * 0.25), 0.1, 0.8)

        return {
            'コード': code,
            '持続性スコア': round(float(persistence_score), 6),
            '決定係数(R2)': round(float(r_squared), 4),
            't値': round(float(t_stat), 2),
            '勝率': round(float(win_prob), 2),
            '推奨株数': int(shares),
            '現在値': curr_p,
            '必要資金': int(need_cash),
            '損切(SL)': round(float(stop_loss), 1),
            '利確(TP)': round(float(take_profit), 1),
            '選定理由': reason_text,
            '推奨保有期間': hold_period,
            '破綻条件': fail_cond
        }


# ==========================================
# 3. JPX銘柄マスター取得
# ==========================================
def get_jpx_master():
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
    try:
        df_m = pd.read_excel(url)
        df_m['JoinCode'] = df_m['コード'].astype(str) + "0"
        return df_m[['JoinCode', '銘柄名', '33業種区分']]
    except:
        return None


# ==========================================
# 4. 実行モジュール（データ読み込み）
# ==========================================
def load_master_data():
    try:
        df = pd.read_parquet(DATA_PATH)
        df['Date'] = pd.to_datetime(df['Date'])
        return df.sort_values(['Code', 'Date'])
    except Exception as e:
        print(f"データ読み込みエラー: {e}")
        return None


# ==========================================
# 5. Portfolio Builder（ブレ封じ：実測EV×制約で自動選定）
# ==========================================
def load_master_2026():
    df = pd.read_csv(MASTER_2026_PATH)
    df['Date'] = pd.to_datetime(df['Date'])
    df['Code'] = df['Code'].astype(str)
    df = df.sort_values(['Code', 'Date'])
    return df

def eval_one_signal(future_df, ep, sl, tp):
    """
    future_df: 予測日の次日以降のN日分（Date昇順）
    日足ではTP/SL両タッチの順序が不明なため、保守的にSL側（worst）扱い。
    """
    latest_close = float(future_df['AdjC'].iloc[-1])
    lowest_low = float(future_df['AdjL'].min())
    highest_high = float(future_df['AdjH'].max())

    hit_sl = (lowest_low <= sl)
    hit_tp = (highest_high >= tp)

    if hit_sl and hit_tp:
        ret_pct = ((sl - ep) / ep) * 100
        state = 'X'
    elif hit_sl:
        ret_pct = ((sl - ep) / ep) * 100
        state = 'SL'
    elif hit_tp:
        ret_pct = ((tp - ep) / ep) * 100
        state = 'TP'
    else:
        ret_pct = ((latest_close - ep) / ep) * 100
        state = 'TIME'

    return state, ret_pct

def attach_empirical_from_history(master_df, picks_df, forward_days=5):
    """
    picks_df（コード・現在値・SL・TP）に対して、2026年データ上で
    “相対幅（SL/TPを%化）”を適用し、全日走査で実測傾向を推定する。
    """
    rows = []
    for _, r in picks_df.iterrows():
        code = str(r['コード'])
        ep0 = float(r['現在値'])
        sl0 = float(r['損切(SL)'])
        tp0 = float(r['利確(TP)'])

        g = master_df[master_df['Code'] == code].sort_values('Date')
        if len(g) < forward_days + 2:
            rows.append({'コード': code, 'emp_n': 0, 'emp_tp': 0, 'emp_sl': 0, 'emp_win': np.nan, 'emp_ret_mean': np.nan})
            continue

        states = []
        rets = []

        # 最後のforward_days分は未来が足りないので除外
        for i in range(0, len(g) - forward_days - 1):
            ep = float(g['AdjC'].iloc[i])

            # 当日のEPに対して、出力されたSL/TPの相対率を適用
            # 例：sl0/ep0 が 0.93 なら当日のslは ep*0.93
            sl = ep * (sl0 / ep0)
            tp = ep * (tp0 / ep0)

            future = g.iloc[i + 1:i + 1 + forward_days]
            state, ret_pct = eval_one_signal(future, ep, sl, tp)
            states.append(state)
            rets.append(ret_pct)

        if len(rets) == 0:
            rows.append({'コード': code, 'emp_n': 0, 'emp_tp': 0, 'emp_sl': 0, 'emp_win': np.nan, 'emp_ret_mean': np.nan})
            continue

        emp_tp = sum(1 for s in states if s == 'TP')
        emp_sl = sum(1 for s in states if s == 'SL')
        emp_win = emp_tp / len(states)  # Xは保守的に勝ち扱いしない
        emp_ret_mean = float(np.mean(rets))

        rows.append({
            'コード': code,
            'emp_n': int(len(states)),
            'emp_tp': int(emp_tp),
            'emp_sl': int(emp_sl),
            'emp_win': float(emp_win),
            'emp_ret_mean': float(emp_ret_mean),
        })

    return pd.DataFrame(rows)

def calc_model_ev(df):
    """
    CSVの勝率・SL・TP・現在値から、1トレード期待リターン(%)を計算
    EV = p*(TP-EP)/EP + (1-p)*(SL-EP)/EP
    """
    p = df['勝率'].astype(float)
    ep = df['現在値'].astype(float)
    sl = df['損切(SL)'].astype(float)
    tp = df['利確(TP)'].astype(float)

    ev = p * ((tp - ep) / ep) * 100 + (1 - p) * ((sl - ep) / ep) * 100
    return ev

def build_portfolio(candidates: pd.DataFrame, max_positions=5):
    """
    ルール：
    - 株数は推奨株数から増やさない
    - 合計必要資金 <= CAPITAL
    - 合計最大損失（SL時） <= TOTAL_RISK_CAP

    目的：
    - EV_blend（%）×（必要資金）で期待“円”を最大化（簡易貪欲）
    """
    df = candidates.copy()

    # 必須列ガード
    required = ['コード', '勝率', '推奨株数', '現在値', '必要資金', '損切(SL)', '利確(TP)']
    for c in required:
        if c not in df.columns:
            raise ValueError(f"portfolio candidateに必要列がありません: {c}")

    df['推奨株数'] = df['推奨株数'].astype(int)
    df['必要資金'] = df['必要資金'].astype(float)
    df['現在値'] = df['現在値'].astype(float)
    df['損切(SL)'] = df['損切(SL)'].astype(float)

    # 1銘柄あたり最大損失（円）
    df['max_loss_yen'] = (df['現在値'] - df['損切(SL)']) * df['推奨株数']

    # モデルEV（%）
    df['EV_model_pct'] = calc_model_ev(df)

    # 実測EV（%）：ここでは保守的に「平均リターン」をEV扱いに近似
    df['emp_ret_mean'] = pd.to_numeric(df.get('emp_ret_mean', np.nan), errors='coerce')
    df['EV_emp_pct'] = df['emp_ret_mean']

    # 実測が弱い場合はモデル寄り
    df['emp_n'] = pd.to_numeric(df.get('emp_n', 0), errors='coerce').fillna(0)
    df.loc[df['emp_n'] < EMP_MIN_N, 'EV_emp_pct'] = np.nan

    # ブレンドEV（%）
    df['EV_blend_pct'] = (1 - EV_BLEND_ALPHA) * df['EV_model_pct'] + EV_BLEND_ALPHA * df['EV_emp_pct'].fillna(df['EV_model_pct'])

    # 期待“円”スコア：EV%×必要資金
    df['EV_yen_score'] = df['EV_blend_pct'] * df['必要資金'] / 100.0

    # 並べ替え
    df = df.sort_values('EV_yen_score', ascending=False)

    picked = []
    cash_used = 0.0
    risk_used = 0.0

    for _, r in df.iterrows():
        if len(picked) >= max_positions:
            break
        if cash_used + r['必要資金'] > CAPITAL:
            continue
        if risk_used + r['max_loss_yen'] > TOTAL_RISK_CAP:
            continue
        if r['推奨株数'] <= 0 or r['必要資金'] <= 0:
            continue
        if np.isnan(r['max_loss_yen']) or r['max_loss_yen'] <= 0:
            continue

        picked.append(r)
        cash_used += float(r['必要資金'])
        risk_used += float(r['max_loss_yen'])

    if len(picked) == 0:
        return pd.DataFrame(), cash_used, risk_used

    out = pd.DataFrame(picked)
    return out, cash_used, risk_used

def run_portfolio_build(asof_yyyymmdd: str, also_save_history: bool = True):
    """
    dailyで生成した strategy_*.csv を読み込み、2026データで実測補正しつつ
    制約条件内でポートフォリオを自動構築してCSV出力する。
    """
    if (not os.path.exists(OUTPUT_S1)) or (not os.path.exists(OUTPUT_S2)):
        print("❌ strategy出力ファイルが見つかりません。dailyを先に実行してください。")
        return

    s1 = pd.read_csv(OUTPUT_S1)
    s2 = pd.read_csv(OUTPUT_S2)
    s1['戦略'] = 'S1'
    s2['戦略'] = 'S2'

    # 型整備
    s1['コード'] = s1['コード'].astype(str)
    s2['コード'] = s2['コード'].astype(str)

    # 候補統合
    cand = pd.concat([s1, s2], ignore_index=True)

    # 2026年データ読み込み
    try:
        master26 = load_master_2026()
    except Exception as e:
        print(f"⚠️ master_2026読み込み失敗（実測補正なしで進行）: {e}")
        master26 = None

    # 実測補正付与（可能なら）
    if master26 is not None:
        uniq = cand[['コード', '現在値', '損切(SL)', '利確(TP)']].drop_duplicates()
        emp = attach_empirical_from_history(master26, uniq, forward_days=FORWARD_DAYS)
        cand = cand.merge(emp, on='コード', how='left')

    # ポートフォリオ構築
    port, cash_used, risk_used = build_portfolio(cand, max_positions=MAX_POSITIONS)

    if port.empty:
        print("❌ 制約条件を満たすポートフォリオが組めませんでした。TOTAL_RISK_CAPやMAX_POSITIONS、EMP_MIN_Nを調整してください。")
        return

    # 出力
    os.makedirs(PREDICT_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)

    out_path = f"{PREDICT_DIR}{PORTFOLIO_OUT_PREFIX}{asof_yyyymmdd}.csv"
    port.to_csv(out_path, index=False, encoding='utf-8-sig')

    if also_save_history:
        history_out = f"{HISTORY_DIR}{PORTFOLIO_OUT_PREFIX}{asof_yyyymmdd}.csv"
        port.to_csv(history_out, index=False, encoding='utf-8-sig')

    # サマリ表示
    print("\n=== 自動構築ポートフォリオ ===")
    show_cols = ['戦略', 'コード', '銘柄名', '推奨株数', '必要資金', 'max_loss_yen', 'EV_model_pct', 'EV_emp_pct', 'EV_blend_pct', 'emp_n', 'emp_tp', 'emp_sl', 'emp_win']
    show_cols = [c for c in show_cols if c in port.columns]

    # 見やすい丸め
    if 'max_loss_yen' in port.columns:
        port['max_loss_yen'] = port['max_loss_yen'].round(0).astype(int)
    for c in ['EV_model_pct', 'EV_emp_pct', 'EV_blend_pct', 'emp_win', 'emp_ret_mean']:
        if c in port.columns:
            port[c] = pd.to_numeric(port[c], errors='coerce').round(2)

    print(port[show_cols] if show_cols else port.head(20))

    print("\n--- サマリ ---")
    print(f"合計必要資金: {int(cash_used)} 円 / 現金余力: {int(CAPITAL - cash_used)} 円")
    print(f"合計最大損失(全SL想定): {int(risk_used)} 円 / リスク上限: {int(TOTAL_RISK_CAP)} 円")
    print(f"✅ 保存: {out_path}")
    if also_save_history:
        print(f"✅ 履歴保存: {history_out}")


# ==========================================
# 6. 日次予測（daily）
# ==========================================
def run_simulation():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 日次予測モード(Daily) 開始...")
    df_all = load_master_data()
    if df_all is None:
        return

    engine = OmnisEngineV5_2()
    s1_results, s2_results = [], []

    for code, group in tqdm(df_all.groupby('Code'), desc="Analyzing"):
        res1 = engine.analyze_strategy_1(group)
        if res1 and res1['推奨株数'] > 0:
            s1_results.append(res1)

        res2 = engine.analyze_strategy_2(group)
        if res2 and res2['推奨株数'] > 0:
            s2_results.append(res2)

    master = get_jpx_master()
    os.makedirs(PREDICT_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    today_str = datetime.now().strftime('%Y%m%d')

    # --- Strategy 1 出力 ---
    df_s1 = pd.DataFrame(s1_results)
    if not df_s1.empty:
        df_s1 = df_s1.sort_values('スコア', ascending=False)
        if master is not None:
            df_s1 = pd.merge(df_s1, master, left_on='コード', right_on='JoinCode', how='left').drop(columns=['JoinCode'])

        df_s1.to_csv(OUTPUT_S1, index=False, encoding='utf-8-sig')                          # 上書き用
        df_s1.to_csv(f"{HISTORY_DIR}s1_{today_str}.csv", index=False, encoding='utf-8-sig') # 履歴用

        print("\n--- Strategy 1 (短期) TOP 5 ---")
        print(df_s1[['コード', 'スコア', '推奨保有期間', '破綻条件']].head(5))
    else:
        print("\n--- Strategy 1 (短期) ---")
        print("該当銘柄なし")

    # --- Strategy 2 出力 ---
    df_s2 = pd.DataFrame(s2_results)
    if not df_s2.empty:
        df_s2 = df_s2.sort_values('持続性スコア', ascending=False)
        if master is not None:
            df_s2 = pd.merge(df_s2, master, left_on='コード', right_on='JoinCode', how='left').drop(columns=['JoinCode'])

        df_s2.to_csv(OUTPUT_S2, index=False, encoding='utf-8-sig')                          # 上書き用
        df_s2.to_csv(f"{HISTORY_DIR}s2_{today_str}.csv", index=False, encoding='utf-8-sig') # 履歴用

        print("\n--- Strategy 2 (長期) TOP 5 ---")
        cols = ['コード', '持続性スコア', '決定係数(R2)', 't値', '推奨保有期間', '破綻条件']
        cols = [c for c in cols if c in df_s2.columns]
        print(df_s2[cols].head(5))
    else:
        print("\n--- Strategy 2 (長期) ---")
        print("該当銘柄なし")

    # --- dailyの最後にポートフォリオ自動生成 ---
    if RUN_PORTFOLIO_AFTER_DAILY:
        try:
            run_portfolio_build(today_str, also_save_history=True)
        except Exception as e:
            print(f"⚠️ ポートフォリオ生成に失敗: {e}")


# ==========================================
# 7. 検証（Evaluator）
# ==========================================
def run_evaluation(target_date_str, forward_days):
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 過去検証モード(Evaluator) 開始...")
    target_path = f"{HISTORY_DIR}s1_{target_date_str}.csv"

    if not os.path.exists(target_path):
        print(f"❌ エラー: 指定された日付の履歴ファイルがありません ({target_path})")
        return

    df_pred = pd.read_csv(target_path)
    df_pred['コード'] = df_pred['コード'].astype(str)

    df_all = load_master_data()
    if df_all is None:
        return

    target_date = pd.to_datetime(target_date_str)

    eval_results = []
    for _, pred in tqdm(df_pred.iterrows(), total=len(df_pred), desc="Evaluating"):
        code = pred['コード']
        ep = float(pred['現在値'])
        sl = float(pred['損切(SL)'])
        tp = float(pred['利確(TP)'])

        future_data = df_all[(df_all['Code'].astype(str) == code) & (df_all['Date'] > target_date)].sort_values('Date').head(forward_days)
        if len(future_data) == 0:
            continue

        latest_close = float(future_data['AdjC'].iloc[-1])
        lowest_low = float(future_data['AdjL'].min())
        highest_high = float(future_data['AdjH'].max())

        hit_sl = (lowest_low <= sl)
        hit_tp = (highest_high >= tp)

        # 日足では両方タッチ順序不明なので別扱い + 損益確定の整合性
        if hit_sl and hit_tp:
            state = "[X] 両方タッチ(順序不明)"
            ret_worst = ((sl - ep) / ep) * 100
            ret_best = ((tp - ep) / ep) * 100
            ret_pct = round(ret_worst, 2)  # 保守的にworst
            ret_best_pct = round(ret_best, 2)

        elif hit_sl:
            state = "[A] 早期決着 (SL到達)"
            ret_pct = round(((sl - ep) / ep) * 100, 2)
            ret_best_pct = np.nan

        elif hit_tp:
            state = "[A] 早期決着 (TP到達)"
            ret_pct = round(((tp - ep) / ep) * 100, 2)
            ret_best_pct = np.nan

        else:
            if latest_close > ep:
                state = "[B] 巡航中 (含み益)"
            else:
                state = "[C] 時間切れ (含み損/停滞)"
            ret_pct = round(((latest_close - ep) / ep) * 100, 2)
            ret_best_pct = np.nan

        eval_results.append({
            'コード': code,
            '予測スコア': pred.get('スコア', np.nan),
            '状態': state,
            'EP(買値)': ep,
            'N日後終値': latest_close,
            'SL到達': hit_sl,
            'TP到達': hit_tp,
            'リターン(%)': ret_pct,
            'リターン_best(%)': ret_best_pct
        })

    df_eval = pd.DataFrame(eval_results)
    if df_eval.empty:
        print("❌ 評価可能な未来のデータがありません。")
        return

    eval_out = f"{HISTORY_DIR}evaluation_s1_{target_date_str}_forward{forward_days}.csv"
    df_eval.to_csv(eval_out, index=False, encoding='utf-8-sig')

    print(f"\n=== IQ1000 評価レポート (予測日: {target_date_str} -> {forward_days}営業日後) ===")
    print(f"対象銘柄数: {len(df_eval)}銘柄")

    state_counts = df_eval['状態'].value_counts()
    for state, count in state_counts.items():
        print(f" - {state}: {count}銘柄 ({count/len(df_eval)*100:.1f}%)")

    top_20 = df_eval.sort_values('予測スコア', ascending=False).head(max(1, int(len(df_eval) * 0.2)))
    print(f"\n全体平均リターン: {df_eval['リターン(%)'].mean():.2f}%")
    print(f"TOP20%平均リターン: {top_20['リターン(%)'].mean():.2f}%")
    print(f"✅ レポート保存完了: {eval_out}")


# ==========================================
# 8. 実行トリガー
# ==========================================
if __name__ == "__main__":
    if RUN_MODE == 'daily':
        run_simulation()
    elif RUN_MODE == 'evaluate':
        run_evaluation(EVAL_TARGET_DATE, FORWARD_DAYS)
    elif RUN_MODE == 'portfolio':
        # strategy_*.csv が既にある前提で、当日分としてportfolioを作る
        today_str = datetime.now().strftime('%Y%m%d')
        run_portfolio_build(today_str, also_save_history=True)
    else:
        print("RUN_MODE を正しく設定してください。")