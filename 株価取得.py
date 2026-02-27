from google.colab import drive
drive.mount('/content/drive')

!pip -q install -U jquants-api-client pandas pyarrow python-dateutil


import os, time
from datetime import datetime, timedelta, date
import pandas as pd
from dateutil import tz
import jquantsapi

# ★ここだけ入れてください（ClientV2用）
JQUANTS_API_KEY = "IYayng6v_Ss5liLof3O5KMxqH0CuQA_FXYi4Lt3Gh68".strip()

BASE_DIR = "/content/drive/MyDrive/JQuantsData"
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