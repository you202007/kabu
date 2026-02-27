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
# ★修正: ファイル名と一致させるために、日付を YYYYMMDD の文字列フォーマットに変換する処理を追加しました
EVAL_TARGET_DATE = (datetime.now() - pd.Timedelta(days=7)).strftime('%Y%m%d') 
FORWARD_DAYS = 5               # 評価期間 (3 または 5 営業日後)

# --- 投資家設定・パス ---
CAPITAL = 1000000              # 総資金 100万円
RISK_PER_TRADE = 20000         # 1トレード許容損失 (2%)
LIQUIDITY_THRESHOLD = 100000000 # 流動性フィルター (1億円)

# ★修正: GitHub Actionsの仮想環境用の相対パスに変更
BASE_DIR = './JQuantsData'
DATA_PATH = os.path.join(BASE_DIR, 'data/master.parquet')
PREDICT_DIR = os.path.join(BASE_DIR, 'predict/')
OUTPUT_S1 = os.path.join(PREDICT_DIR, 'strategy_1_spear_ride.csv')
OUTPUT_S2 = os.path.join(PREDICT_DIR, 'strategy_2_persistence.csv')

# 履歴（スナップショット）保存先
HISTORY_DIR = os.path.join(PREDICT_DIR, 'history/')

# --- daily実行後にポートフォリオも自動生成する ---
RUN_PORTFOLIO_AFTER_DAILY = True

# --- Portfolio Builder用 ---
# ★修正: こちらも仮想環境用の相対パスに変更
MASTER_2026_PATH = os.path.join(BASE_DIR, 'data/master_2026.csv')  
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
            np.maximum(np.abs(high[1:] - close
