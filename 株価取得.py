import os, time
from datetime import datetime, timedelta, date
import pandas as pd
from dateutil import tz
import jquantsapi

# ----------------------------------------
# 1. APIキーの読み込み（直接書かずにSecretsから読む）
# ----------------------------------------

# GitHub Actionsから渡された環境変数を受け取る
JQUANTS_API_KEY = os.environ.get("API_KEY")

if JQUANTS_API_KEY is None:
    print("エラー: シークレットキーが見つかりません！")
else:
    print("キーを安全に取得できました。処理を開始します...")
    
# ----------------------------------------
# 2. 保存先フォルダを「GitHubリポジトリ内」に設定
# ----------------------------------------
# ★保存先をGitHub Actionsの仮想環境内のフォルダに変更
BASE_DIR = "./JQuantsData"
DATA_DIR = os.path.join(BASE_DIR, "data")
RUNS_DIR = os.path.join(DATA_DIR, "runs")

MASTER_PATH = os.path.join(DATA_DIR, "master.parquet")
LAST_PATH = os.path.join(DATA_DIR, "last_update.txt")

os.makedirs(RUNS_DIR, exist_ok=True)
JST = tz.gettz("Asia/Tokyo")

print("BASE_DIR:", BASE_DIR)
print("MASTER_PATH:", MASTER_PATH)
print("LAST_PATH:", LAST_PATH)

cli = jquantsapi.ClientV2(api_key=JQUANTS_API_KEY)
print("ClientV2 OK")

# ----------------------------------------
# 3. 関数定義
# ----------------------------------------
def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, 1)

def end_of_month(d: date) -> date:
    nm = add_months(date(d.year, d.month, 1), 1)
    return nm - timedelta(days=1)

def quarter_chunks(start_day: date, end_day: date, step_months: int = 3):
    """
    start_day〜end_dayを step_months（デフォ3）か月単位で区切る
    """
    cur = start_day
    while cur <= end_day:
        cur_month_start = date(cur.year, cur.month, 1)
        next_block_start = add_months(cur_month_start, step_months)
        block_end = min(end_day, next_block_start - timedelta(days=1))
        yield cur, block_end
        cur = block_end + timedelta(days=1)

def safe_get_range(cli, start_day: date, end_day: date, max_retries: int = 10) -> pd.DataFrame:
    """
    429が出たら指数バックオフして再試行。その他は例外。
    """
    start_dt = datetime(start_day.year, start_day.month, start_day.day, tzinfo=JST)
    end_dt   = datetime(end_day.year,   end_day.month,   end_day.day,   tzinfo=JST)

    attempt = 0
    while True:
        try:
            df = cli.get_eq_bars_daily_range(start_dt=start_dt, end_dt=end_dt)
            return df
        except Exception as e:
            msg = str(e).lower()
            is_429 = ("429" in msg) or ("too many" in msg) or ("rate" in msg and "limit" in msg)
            if not is_429:
                raise
            if attempt >= max_retries:
                raise
            sleep_s = min(120, 2 ** attempt)  # 1,2,4,...,120
            print(f"429 hit. sleep {sleep_s}s then retry... ({attempt+1}/{max_retries})")
            time.sleep(sleep_s)
            attempt += 1

def ensure_last_file():
    if not os.path.exists(LAST_PATH):
        with open(LAST_PATH, "w", encoding="utf-8") as f:
            f.write("")
    with open(LAST_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()

def write_last(date_str: str):
    with open(LAST_PATH, "w", encoding="utf-8") as f:
        f.write(date_str)

def merge_and_save(master_path: str, new_df: pd.DataFrame) -> pd.DataFrame:
    if os.path.exists(master_path):
        master_df = pd.read_parquet(master_path)
        combined = pd.concat([master_df, new_df], ignore_index=True)
    else:
        combined = new_df

    # 念のため重複排除（Code+Date）
    if "Code" in combined.columns and "Date" in combined.columns:
        combined.drop_duplicates(subset=["Code", "Date"], keep="last", inplace=True)

    combined.to_parquet(master_path, index=False)
    return combined

# ----------------------------------------
# 4. メイン処理
# ----------------------------------------
last_str = ensure_last_file()
today = date.today()

if last_str == "":
    start_day = today - timedelta(days=730)  # 初回2年
    mode = "FIRST_RUN_2Y"
else:
    last_day = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S").date()
    start_day = last_day + timedelta(days=1)
    mode = "INCREMENTAL"

end_day = today
print(f"[{mode}] {start_day} -> {end_day}")

run_date = end_day.strftime("%Y-%m-%d")
print("RUN DATE:", run_date)

total_rows = 0

for s, e in quarter_chunks(start_day, end_day, step_months=3):
    print(f"\n=== chunk {s} -> {e} ===")

    # 取得
    df = safe_get_range(cli, s, e)

    if df is None or df.empty:
        print("no data in this chunk (holiday-only or not updated yet)")
        continue

    # 今回チャンクをrunsに保存（復旧用）
    chunk_tag = f"{s.strftime('%Y%m%d')}_{e.strftime('%Y%m%d')}"
    run_path = os.path.join(RUNS_DIR, f"daily_{run_date}_chunk_{chunk_tag}.parquet")
    df.to_parquet(run_path, index=False)
    print("saved chunk:", run_path, "rows=", len(df))

    # masterにマージして保存（チェックポイント）
    combined = merge_and_save(MASTER_PATH, df)
    total_rows = len(combined)

    # last_update を「取得できた最大Date」で更新（欠損防止）
    latest = str(combined["Date"].max())
    write_last(latest)
    print("checkpoint last_update:", latest, "| master rows:", total_rows)

print("\nDONE. master rows:", total_rows)
print("last_update:", ensure_last_file())

# ----------------------------------------
# 5. CSVファイルの出力（ローカルパスに修正）
# ----------------------------------------
master_df = pd.read_parquet(MASTER_PATH)
csv_path = MASTER_PATH.replace(".parquet", ".csv")
master_df.to_csv(csv_path, index=False)
print("CSV saved:", csv_path)

master_df_csv = pd.read_csv(csv_path)
master_df_csv['Date'] = pd.to_datetime(master_df_csv['Date'])

output_dir = DATA_DIR # 保存先をGitHubのディレクトリに指定

years_to_filter = [2024, 2025, 2026]

for year in years_to_filter:
    # Filter data for the current year
    df_year = master_df_csv[master_df_csv['Date'].dt.year == year]

    # Construct the output file path
    output_file_path = os.path.join(output_dir, f'master_{year}.csv')

    # Save the filtered DataFrame to CSV
    df_year.to_csv(output_file_path, index=False)
    print(f"Saved data for year {year} to: {output_file_path}")

print("All yearly CSV files have been created.")