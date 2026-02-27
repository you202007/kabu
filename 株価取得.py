import os, time
from datetime import datetime, timedelta, date
import pandas as pd
from dateutil import tz
import jquantsapi

# GitHub Actionsから渡された環境変数を受け取る
JQUANTS_API_KEY = os.environ.get("API_KEY")

if JQUANTS_API_KEY is None:
    print("エラー: シークレットキーが見つかりません！")
else:
    print("キーを安全に取得できました。処理を開始します...")

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