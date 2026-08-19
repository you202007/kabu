"""Fetch market data for the SQ distortion dashboard.

The fetcher intentionally separates two data classes:

- Prices and index data: fetched from Yahoo Finance via yfinance.
- Nikkei 225 option open interest: read from CSV when available. If no option
  CSV is supplied, a clearly marked proxy option surface is generated so the
  dashboard and GitHub Pages update flow remain operational.

The proxy option surface is not suitable for trading decisions. It is a
placeholder until JPX/OSE option open-interest data is connected.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from sq_distortion_model import ModelConfig, run_pipeline

try:
    from jquantsapi.client_v2 import ClientV2 as JQuantsClient
except ImportError:  # pragma: no cover - optional dependency path
    JQuantsClient = None


TRACKED_COMPONENTS = [
    {"ticker": "285A", "yf": "285A.T", "name": "キオクシア", "adj_factor": 0.82},
    {"ticker": "5803", "yf": "5803.T", "name": "フジクラ", "adj_factor": 0.70},
    {"ticker": "6857", "yf": "6857.T", "name": "アドバンテスト", "adj_factor": 2.10},
    {"ticker": "9984", "yf": "9984.T", "name": "ソフトバンクG", "adj_factor": 3.00},
    {"ticker": "8035", "yf": "8035.T", "name": "東京エレクトロン", "adj_factor": 1.40},
    {"ticker": "9983", "yf": "9983.T", "name": "ファーストリテイリング", "adj_factor": 2.70},
    {"ticker": "6920", "yf": "6920.T", "name": "レーザーテック", "adj_factor": 0.90},
    {"ticker": "6146", "yf": "6146.T", "name": "ディスコ", "adj_factor": 1.00},
    {"ticker": "6098", "yf": "6098.T", "name": "リクルート", "adj_factor": 1.00},
    {"ticker": "4063", "yf": "4063.T", "name": "信越化学", "adj_factor": 1.00},
    {"ticker": "4519", "yf": "4519.T", "name": "中外製薬", "adj_factor": 1.00},
    {"ticker": "7203", "yf": "7203.T", "name": "トヨタ自動車", "adj_factor": 1.00},
    {"ticker": "9433", "yf": "9433.T", "name": "KDDI", "adj_factor": 1.00},
    {"ticker": "6762", "yf": "6762.T", "name": "TDK", "adj_factor": 1.00},
    {"ticker": "4543", "yf": "4543.T", "name": "テルモ", "adj_factor": 1.00},
    {"ticker": "6367", "yf": "6367.T", "name": "ダイキン工業", "adj_factor": 1.00},
]


def second_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 7)


def next_months(start: date, count: int) -> list[tuple[int, int]]:
    months = []
    year = start.year
    month = start.month
    for _ in range(count):
        months.append((year, month))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return months


def make_sq_calendar(out_dir: Path, today: date) -> pd.DataFrame:
    rows = []
    for year, month in next_months(today.replace(day=1), 12):
        sq = second_friday(year, month)
        rows.append(
            {
                "sq_date": sq.isoformat(),
                "kind": "major" if month in {3, 6, 9, 12} else "minor",
            }
        )
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "sq_calendar.csv", index=False, encoding="utf-8")
    return df


def download_symbol(symbol: str, period: str) -> pd.DataFrame:
    df = yf.download(symbol, period=period, auto_adjust=False, progress=False, threads=False)
    if df.empty:
        raise RuntimeError(f"No Yahoo Finance data returned for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]
    return df.reset_index()


def fetch_constituents(out_dir: Path, period: str) -> pd.DataFrame:
    rows = []
    for component in TRACKED_COMPONENTS:
        hist = download_symbol(component["yf"], period)
        hist["Date"] = pd.to_datetime(hist["Date"]).dt.date
        hist = hist.sort_values("Date")
        hist["prev_close"] = hist["Close"].shift(1)
        for _, row in hist.dropna(subset=["prev_close"]).iterrows():
            rows.append(
                {
                    "date": row["Date"].isoformat(),
                    "ticker": component["ticker"],
                    "name": component["name"],
                    "close": float(row["Close"]),
                    "prev_close": float(row["prev_close"]),
                    "adj_factor": component["adj_factor"],
                    "volume": int(row.get("Volume", 0) or 0),
                }
            )
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "constituents_daily.csv", index=False, encoding="utf-8")
    return df


def atr_from_history(hist: pd.DataFrame, window: int = 14) -> pd.Series:
    high_low = hist["High"] - hist["Low"]
    high_prev = (hist["High"] - hist["Close"].shift(1)).abs()
    low_prev = (hist["Low"] - hist["Close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_prev, low_prev], axis=1).max(axis=1)
    return true_range.rolling(window, min_periods=3).mean().bfill()


def fetch_index_daily(
    out_dir: Path,
    constituents: pd.DataFrame,
    period: str,
    topix_csv: Path | None = None,
) -> pd.DataFrame:
    nikkei = download_symbol("^N225", period)
    topix_override_dates: set[date] = set()
    try:
        topix = download_symbol("^TOPX", period)
        topix_source = "Yahoo Finance ^TOPX"
    except RuntimeError:
        try:
            # 1306.T is the NEXT FUNDS TOPIX ETF. It is not the index itself,
            # but it is a better breadth proxy than scaling Nikkei 225.
            topix = download_symbol("1306.T", period)
            topix_source = "Yahoo Finance 1306.T TOPIX ETF proxy"
        except RuntimeError:
            topix = nikkei.copy()
            topix[["Open", "High", "Low", "Close"]] = topix[["Open", "High", "Low", "Close"]] / 20
            topix_source = "Nikkei scaled fallback"
    if topix_csv:
        topix_override = pd.read_csv(topix_csv)
        topix_override = topix_override.rename(columns={"O": "Open", "H": "High", "L": "Low", "C": "Close"})
        topix["Date"] = pd.to_datetime(topix["Date"]).dt.date
        topix_override["Date"] = pd.to_datetime(topix_override["Date"]).dt.date
        topix_override_dates = set(topix_override["Date"])
        topix = topix.sort_values("Date")
        topix["__base_close"] = topix["Close"]
        topix["__base_prev_close"] = topix["Close"].shift(1)
        topix = (
            topix.set_index("Date")
            .combine_first(topix_override.set_index("Date"))
            .reset_index()
        )
        for col in ["Open", "High", "Low", "Close"]:
            override_map = topix_override.set_index("Date")[col]
            topix[col] = topix["Date"].map(override_map).fillna(topix[col])
        topix_source = f"{topix_source}; J-Quants override {topix_csv}"
    else:
        topix = topix.sort_values("Date")
        topix["__base_close"] = topix["Close"]
        topix["__base_prev_close"] = topix["Close"].shift(1)

    nikkei["Date"] = pd.to_datetime(nikkei["Date"]).dt.date
    topix["Date"] = pd.to_datetime(topix["Date"]).dt.date
    nikkei = nikkei.sort_values("Date")
    topix = topix.sort_values("Date")

    idx = nikkei[["Date", "Close", "High", "Low"]].rename(
        columns={"Date": "date", "Close": "nikkei_close"}
    )
    idx["nikkei_prev_close"] = idx["nikkei_close"].shift(1)
    idx["nikkei_atr"] = atr_from_history(nikkei)
    tp = topix[["Date", "Close", "__base_close", "__base_prev_close"]].rename(
        columns={"Date": "date", "Close": "topix_close"}
    )
    tp["topix_prev_close"] = tp["topix_close"].shift(1)
    if topix_override_dates:
        needs_level_adjustment = tp["date"].isin(topix_override_dates) & ~tp["date"].shift(1).isin(topix_override_dates)
        needs_reverse_adjustment = ~tp["date"].isin(topix_override_dates) & tp["date"].shift(1).isin(topix_override_dates)
        base_return = tp["__base_close"] / tp["__base_prev_close"] - 1
        adjusted_prev = tp["topix_close"] / (1 + base_return)
        tp.loc[needs_level_adjustment & np.isfinite(adjusted_prev), "topix_prev_close"] = adjusted_prev
        tp.loc[needs_reverse_adjustment & np.isfinite(adjusted_prev), "topix_prev_close"] = adjusted_prev
    tp = tp.drop(columns=["__base_close", "__base_prev_close"])
    idx = idx.merge(tp, on="date", how="inner")

    cons = constituents.copy()
    cons["is_advancing"] = cons["close"] > cons["prev_close"]
    breadth = cons.groupby("date", as_index=False)["is_advancing"].mean()
    breadth["date"] = pd.to_datetime(breadth["date"]).dt.date
    breadth = breadth.rename(columns={"is_advancing": "advancers_ratio"})
    idx = idx.merge(breadth, on="date", how="left")
    idx["advancers_ratio"] = idx["advancers_ratio"].fillna(0.5)

    # A robust free Nikkei futures source is not guaranteed. Use spot as the
    # baseline until OSE futures data is connected.
    idx["futures_close"] = idx["nikkei_close"]
    out = idx.dropna(subset=["nikkei_prev_close", "topix_prev_close"])[
        [
            "date",
            "nikkei_close",
            "nikkei_prev_close",
            "topix_close",
            "topix_prev_close",
            "nikkei_atr",
            "futures_close",
            "advancers_ratio",
        ]
    ]
    out.to_csv(out_dir / "index_daily.csv", index=False, encoding="utf-8")
    (out_dir / "index_source_metadata.json").write_text(
        json.dumps({"topix_source": topix_source}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


def nearest_sq(sq_calendar: pd.DataFrame, current: date) -> date:
    dates = sorted(pd.to_datetime(sq_calendar["sq_date"]).dt.date)
    future = [d for d in dates if d >= current]
    return future[0] if future else dates[-1]


def make_proxy_options(out_dir: Path, index_daily: pd.DataFrame, sq_calendar: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in index_daily.iterrows():
        current = pd.to_datetime(row["date"]).date()
        expiry = nearest_sq(sq_calendar, current)
        nikkei = float(row["nikkei_close"])
        atr = max(float(row["nikkei_atr"]), 350)
        center = int(round(nikkei / 500) * 500)
        strikes = range(center - 4000, center + 4500, 500)
        for strike in strikes:
            distance = abs(strike - nikkei) / atr
            base = 6000 * math.exp(-0.35 * distance)
            call_tilt = 1.0 + max(0, strike - nikkei) / 5000
            put_tilt = 1.0 + max(0, nikkei - strike) / 5000
            rows.append(
                {
                    "date": current.isoformat(),
                    "expiry": expiry.isoformat(),
                    "type": "C",
                    "strike": strike,
                    "open_interest": int(base * call_tilt),
                }
            )
            rows.append(
                {
                    "date": current.isoformat(),
                    "expiry": expiry.isoformat(),
                    "type": "P",
                    "strike": strike,
                    "open_interest": int(base * put_tilt),
                }
            )
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "options_oi.csv", index=False, encoding="utf-8")
    return df


def write_options_file(df: pd.DataFrame, out_dir: Path) -> None:
    required = {"date", "expiry", "type", "strike", "open_interest"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"options_oi.csv missing columns: {', '.join(missing)}")
    df[["date", "expiry", "type", "strike", "open_interest"]].to_csv(
        out_dir / "options_oi.csv",
        index=False,
        encoding="utf-8",
    )


def normalize_options_file(raw: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "expiry", "type", "strike", "open_interest"}
    if required.issubset(raw.columns):
        return raw[["date", "expiry", "type", "strike", "open_interest"]].copy()
    if {"Date", "OI", "Strike", "PCDiv"}.issubset(raw.columns):
        return normalize_jquants_options(raw)
    raise ValueError(
        "Options CSV must be normalized options_oi.csv or J-Quants "
        f"derivatives_bars_daily_options_225 CSV. Returned columns: {', '.join(map(str, raw.columns))}"
    )


def copy_options(options_csv: Path, out_dir: Path) -> None:
    raw = pd.read_csv(options_csv)
    write_options_file(normalize_options_file(raw), out_dir)


def download_options_csv(options_url: str, out_dir: Path) -> None:
    df = pd.read_csv(options_url)
    write_options_file(normalize_options_file(df), out_dir)


def first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    normalized = {str(col).lower().replace("_", ""): col for col in df.columns}
    for candidate in candidates:
        key = candidate.lower().replace("_", "")
        if key in normalized:
            return normalized[key]
    return None


def normalize_jquants_options(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=["date", "expiry", "type", "strike", "open_interest"])

    date_col = first_existing_column(raw, ["Date", "date"])
    expiry_col = first_existing_column(
        raw,
        ["SQD", "ContractMonth", "ContractMonthDate", "LastTradingDay", "ExerciseDate", "ExpirationDate", "Expiry"],
    )
    type_col = first_existing_column(
        raw,
        ["PCDiv", "PutCallDivision", "PutCall", "OptionType", "Type", "CallPutDivision"],
    )
    strike_col = first_existing_column(raw, ["StrikePrice", "ExercisePrice", "Strike", "strike"])
    oi_col = first_existing_column(
        raw,
        ["OpenInterest", "OpenInterestVolume", "OutstandingVolume", "OpenInterestQty", "OI"],
    )

    missing = [
        label
        for label, col in {
            "date": date_col,
            "type": type_col,
            "strike": strike_col,
            "open_interest": oi_col,
        }.items()
        if col is None
    ]
    if missing:
        raise ValueError(
            "J-Quants options data could not be mapped to options_oi.csv. "
            f"Missing: {', '.join(missing)}. Returned columns: {', '.join(map(str, raw.columns))}"
        )

    out = pd.DataFrame()
    out["date"] = pd.to_datetime(raw[date_col]).dt.strftime("%Y-%m-%d")
    if expiry_col is None:
        out["expiry"] = out["date"]
    else:
        expiry = raw[expiry_col].astype(str).str.replace("/", "-", regex=False)
        yyyymm = expiry.str.fullmatch(r"\d{6}")
        parsed = pd.to_datetime(expiry.where(~yyyymm, expiry + "01"), errors="coerce")
        out["expiry"] = parsed.dt.strftime("%Y-%m-%d").fillna(expiry)

    opt_type = raw[type_col].astype(str).str.upper().str.strip()
    type_key = str(type_col).lower().replace("_", "")
    if type_key in {"pcdiv", "putcalldivision", "callputdivision"}:
        out["type"] = np.select(
            [opt_type.isin(["1", "01"]), opt_type.isin(["2", "02"])],
            ["P", "C"],
            default=opt_type.str[0],
        )
    else:
        out["type"] = np.select(
            [
                opt_type.str.contains("CALL") | opt_type.isin(["C"]),
                opt_type.str.contains("PUT") | opt_type.isin(["P"]),
            ],
            ["C", "P"],
            default=opt_type.str[0],
        )
    out["strike"] = pd.to_numeric(raw[strike_col], errors="coerce")
    out["open_interest"] = pd.to_numeric(raw[oi_col], errors="coerce")
    out = out.dropna(subset=["date", "type", "strike", "open_interest"])
    out = out[out["type"].isin(["C", "P"])]
    return out


def fetch_jquants_options(
    out_dir: Path,
    dates: pd.Series,
    *,
    api_key: str | None = None,
) -> None:
    if JQuantsClient is None:
        raise RuntimeError("jquants-api-client is not installed.")
    if not api_key:
        raise RuntimeError("J-Quants API key is required.")
    client = JQuantsClient(api_key=api_key)
    requested_dates = pd.to_datetime(dates, errors="coerce").dropna()
    if requested_dates.empty:
        raise RuntimeError("No valid dates were available for the J-Quants request.")

    raw = client.get_drv_bars_daily_opt_225_range(
        start_dt=requested_dates.min().strftime("%Y%m%d"),
        end_dt=requested_dates.max().strftime("%Y%m%d"),
    )
    if raw.empty:
        raise RuntimeError("J-Quants returned no Nikkei 225 option data for the requested dates.")
    options = normalize_jquants_options(raw)
    if options.empty:
        raise RuntimeError("J-Quants option data could not be normalized.")
    options.to_csv(out_dir / "options_oi.csv", index=False, encoding="utf-8")


def resolve_jquants_api_key(cli_value: str | None) -> str | None:
    """Prefer an explicit CLI value, otherwise use the standard V2 environment variable."""
    return cli_value or os.environ.get("JQUANTS_API_KEY")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch market data and build SQ dashboard inputs.")
    parser.add_argument("--input-dir", type=Path, default=Path("work/input_data"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--period", default="3mo")
    parser.add_argument("--options-csv", type=Path)
    parser.add_argument("--options-url", help="HTTP(S) URL for a real options_oi.csv file.")
    parser.add_argument("--topix-csv", type=Path, help="J-Quants indices_bars_daily_topix CSV or CSV.GZ.")
    parser.add_argument(
        "--jquants-api-key",
        help="J-Quants V2 API key. Prefer the JQUANTS_API_KEY environment variable.",
    )
    parser.add_argument(
        "--allow-proxy-options",
        action="store_true",
        help="Generate a clearly marked proxy option OI surface when no real options CSV is supplied.",
    )
    parser.add_argument("--sector-config", type=Path, default=Path("work/config/sector_baskets.json"))
    parser.add_argument(
        "--history-path",
        type=Path,
        default=Path("data/sq_score_history.csv"),
        help="Persisted, git-tracked archive of daily scores for later threshold audits.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.input_dir.mkdir(parents=True, exist_ok=True)
    # yfinance uses SQLite-backed caches. Keeping the cache under the writable
    # input directory avoids failures in restricted runners and containers.
    yf_cache_dir = args.input_dir / ".yfinance-cache"
    yf_cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(yf_cache_dir))
    metadata = {
        "price_source": "Yahoo Finance via yfinance",
        "constituent_mode": "tracked high-impact Nikkei 225 basket",
        "option_oi_source": None,
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
    }

    constituents = fetch_constituents(args.input_dir, args.period)
    index_daily = fetch_index_daily(args.input_dir, constituents, args.period, args.topix_csv)
    sq_calendar = make_sq_calendar(args.input_dir, date.today())

    jquants_api_key = resolve_jquants_api_key(args.jquants_api_key)
    if jquants_api_key:
        fetch_jquants_options(
            args.input_dir,
            index_daily["date"],
            api_key=jquants_api_key,
        )
        metadata["option_oi_source"] = "J-Quants /derivatives/bars/daily/options/225"
        metadata["option_oi_is_proxy"] = False
    elif args.options_url:
        download_options_csv(args.options_url, args.input_dir)
        metadata["option_oi_source"] = args.options_url
        metadata["option_oi_is_proxy"] = False
    elif args.options_csv:
        copy_options(args.options_csv, args.input_dir)
        metadata["option_oi_source"] = str(args.options_csv)
        metadata["option_oi_is_proxy"] = False
    elif args.allow_proxy_options:
        make_proxy_options(args.input_dir, index_daily, sq_calendar)
        metadata["option_oi_source"] = "proxy generated from Nikkei level and ATR"
        metadata["option_oi_is_proxy"] = True
    elif (args.input_dir / "options_oi.csv").exists():
        metadata["option_oi_source"] = str(args.input_dir / "options_oi.csv")
        metadata["option_oi_is_proxy"] = False
    else:
        raise RuntimeError(
            "No options_oi.csv found. Supply --options-csv or use --allow-proxy-options "
            "for dashboard-only placeholder data."
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "data_source_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    run_pipeline(args.input_dir, args.output_dir, ModelConfig(), args.sector_config, args.history_path)


if __name__ == "__main__":
    main()
