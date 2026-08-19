"""SQ distortion scoring pipeline for Nikkei 225.

This script turns daily index, constituent contribution, option open interest,
and SQ calendar CSVs into:

- daily SQ distortion scores
- top contribution tables
- a static HTML dashboard

It can also generate sample CSVs so the pipeline can be exercised before real
market data is connected.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


REQUIRED_CONSTITUENT_COLUMNS = {
    "date",
    "ticker",
    "name",
    "close",
    "prev_close",
    "adj_factor",
    "volume",
}

REQUIRED_INDEX_COLUMNS = {
    "date",
    "nikkei_close",
    "nikkei_prev_close",
    "topix_close",
    "topix_prev_close",
    "nikkei_atr",
    "futures_close",
    "advancers_ratio",
}

REQUIRED_OPTION_COLUMNS = {
    "date",
    "expiry",
    "type",
    "strike",
    "open_interest",
}

REQUIRED_SQ_COLUMNS = {"sq_date", "kind"}


@dataclass(frozen=True)
class ModelConfig:
    top_k: int = 10
    momentum_days: int = 5
    sq_window_days: int = 5
    post_sq_days: int = 3
    up_threshold: float = 0.60
    down_threshold: float = 0.40
    # 全期間z-scoreは日々の再計算で過去日の判定まで書き換わってしまうため、
    # 直近N営業日のtrailing windowでz-score化する。252営業日=約1年（SQ12回分）。
    zscore_window: int = 252
    zscore_min_periods: int = 20
    # 改修3: 上方向SQ圧力と下方向剥落リスクの乖離が一定以上ある「中立」判定を
    # より情報量のあるラベルに倒すための閾値。basis除外・rolling z-score適用後の
    # 実データ（2024/08-2026/08、487営業日、p_upが中立域0.40-0.60の203日）で
    # |up_down_gap| の分布はp75=0.31・p80=0.38・p90=0.45。p80付近を採用。
    asym_gap_threshold: float = 0.40
    # 改修2: 半導体バスケット（追跡15銘柄中5銘柄）の寄与度シェア（当日の指数変動の
    # うち半導体が占める割合）の警告色境界。同じ実データでの分布はp50=0.57・
    # p75=0.70・p90=0.79。p75/p90を採用。
    sector_abs_share_watch: float = 0.70
    sector_abs_share_alert: float = 0.80


def zscore(series: pd.Series) -> pd.Series:
    """Whole-sample z-score. Kept for callers that intentionally want a fixed
    baseline (e.g. one-off diagnostics). Score composition should use
    rolling_zscore so a past day's score does not change as new days arrive.
    """
    values = pd.to_numeric(series, errors="coerce")
    std = values.std(ddof=0)
    if not np.isfinite(std) or std == 0:
        return pd.Series(np.zeros(len(values)), index=series.index)
    return (values - values.mean()) / std


def rolling_zscore(series: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Trailing-window z-score. Because the window only looks backward, a
    given date's score is stable across reruns even as new days are appended
    — unlike a whole-sample z-score, which shifts every past day's score
    whenever the sample grows.
    """
    values = pd.to_numeric(series, errors="coerce")
    mean = values.rolling(window, min_periods=min_periods).mean()
    std = values.rolling(window, min_periods=min_periods).std(ddof=0)
    z = (values - mean) / std.replace(0, np.nan)
    return z.fillna(0.0)


def logistic(value: pd.Series | float) -> pd.Series | float:
    return 1 / (1 + np.exp(-value))


def require_columns(df: pd.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"{label} is missing required columns: {', '.join(missing)}")


def read_csv(path: Path, required: set[str], label: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    require_columns(df, required, label)
    return df


def business_day_distance(dates: pd.Series, sq_dates: Iterable[pd.Timestamp]) -> pd.DataFrame:
    sq_dates = sorted(pd.to_datetime(list(sq_dates)))
    rows: list[dict[str, object]] = []
    all_dates = pd.Series(pd.to_datetime(dates)).sort_values().drop_duplicates().reset_index(drop=True)
    known = list(all_dates)

    for current in known:
        future = [d for d in sq_dates if d >= current]
        past = [d for d in sq_dates if d < current]
        next_sq = future[0] if future else sq_dates[-1]
        previous_sq = past[-1] if past else pd.NaT
        if next_sq > current:
            # Count business days in (current, next_sq]. The in-sample `known` dates
            # only cover history (<= latest), so a "known"-based count is always 0 for
            # the latest row. Use a calendar count so time_pressure is correct.
            days_to_sq = int(
                np.busday_count(
                    (current + pd.Timedelta(days=1)).date(),
                    (next_sq + pd.Timedelta(days=1)).date(),
                )
            )
        else:
            days_to_sq = 0
        days_after_sq = sum(1 for d in known if pd.notna(previous_sq) and previous_sq < d <= current)
        rows.append(
            {
                "date": current,
                "next_sq_date": next_sq,
                "previous_sq_date": previous_sq,
                "days_to_sq": days_to_sq,
                "days_after_sq": days_after_sq if pd.notna(previous_sq) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def compute_contributions(constituents: pd.DataFrame, cfg: ModelConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = constituents.copy()
    df["date"] = pd.to_datetime(df["date"])
    numeric_cols = ["close", "prev_close", "adj_factor", "volume"]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    df = df.sort_values(["ticker", "date"])

    # The divisor cancels out for concentration and trend. Real divisor can be
    # added later if point-level index contribution is required.
    df["adjusted_price"] = df["adj_factor"] * df["close"]
    df["contribution_raw"] = df["adj_factor"] * (df["close"] - df["prev_close"])
    df["abs_contribution_raw"] = df["contribution_raw"].abs()
    df["momentum"] = df.groupby("ticker")["close"].pct_change(cfg.momentum_days)

    daily_sum = df.groupby("date")["adjusted_price"].transform("sum")
    df["index_weight"] = np.where(daily_sum != 0, df["adjusted_price"] / daily_sum, 0)
    df["weighted_momentum"] = df["index_weight"] * df["momentum"].fillna(0)

    ranked = df.sort_values(["date", "abs_contribution_raw"], ascending=[True, False]).copy()
    ranked["contribution_rank"] = ranked.groupby("date").cumcount() + 1
    top = ranked[ranked["contribution_rank"] <= cfg.top_k].copy()

    agg_all = df.groupby("date", as_index=False).agg(
        total_abs_contribution=("abs_contribution_raw", "sum"),
        contribution_trend=("weighted_momentum", "sum"),
    )
    agg_top = top.groupby("date", as_index=False).agg(
        top_contribution=("contribution_raw", "sum"),
        top_abs_contribution=("abs_contribution_raw", "sum"),
    )
    daily = agg_all.merge(agg_top, on="date", how="left")
    daily["top_contribution"] = daily["top_contribution"].fillna(0)
    daily["top_abs_contribution"] = daily["top_abs_contribution"].fillna(0)
    daily["contribution_concentration"] = np.where(
        daily["total_abs_contribution"] != 0,
        daily["top_abs_contribution"] / daily["total_abs_contribution"],
        0,
    )
    return daily, ranked


def load_sector_baskets(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def compute_sector_exposure(ranked: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Daily aggregate exposure of a ticker basket within the tracked universe.

    sector_abs_share is the fraction of the day's total |contribution| that
    the basket accounts for — i.e. how much of today's index move is coming
    from the basket, regardless of a separate "avoid the basket individually"
    decision. This is a visibility metric, not a trading signal.
    """
    df = ranked.copy()
    df["date"] = pd.to_datetime(df["date"])
    total_abs = df.groupby("date")["contribution_raw"].apply(lambda s: s.abs().sum())
    basket = df[df["ticker"].isin(tickers)]
    if basket.empty:
        return pd.DataFrame(columns=["date", "sector_contribution", "sector_abs_share", "sector_weight"])
    sector_sum = basket.groupby("date")["contribution_raw"].sum()
    sector_abs_sum = basket.groupby("date")["contribution_raw"].apply(lambda s: s.abs().sum())
    sector_weight = basket.groupby("date")["index_weight"].sum()
    out = pd.DataFrame(
        {
            "sector_contribution": sector_sum,
            "sector_abs_share": sector_abs_sum / total_abs.reindex(sector_abs_sum.index).replace(0, np.nan),
            "sector_weight": sector_weight,
        }
    ).reset_index()
    out["sector_abs_share"] = out["sector_abs_share"].fillna(0.0)
    return out


def compute_option_features(options: pd.DataFrame, index_daily: pd.DataFrame) -> pd.DataFrame:
    opt = options.copy()
    opt["date"] = pd.to_datetime(opt["date"])
    opt["expiry"] = pd.to_datetime(opt["expiry"])
    opt["type"] = opt["type"].str.upper().str[0]
    opt[["strike", "open_interest"]] = opt[["strike", "open_interest"]].apply(pd.to_numeric, errors="coerce")

    idx = index_daily[["date", "nikkei_close", "nikkei_atr"]].copy()
    idx["date"] = pd.to_datetime(idx["date"])
    merged = opt.merge(idx, on="date", how="inner")
    merged["atr_safe"] = merged["nikkei_atr"].replace(0, np.nan).fillna(merged["nikkei_atr"].median())
    merged["distance"] = (merged["nikkei_close"] - merged["strike"]).abs() / merged["atr_safe"]
    merged["magnet_weight"] = merged["open_interest"] * np.exp(-merged["distance"])
    merged["call_magnet"] = np.where(
        (merged["type"] == "C") & (merged["strike"] >= merged["nikkei_close"]),
        merged["magnet_weight"],
        0,
    )
    merged["put_magnet"] = np.where(
        (merged["type"] == "P") & (merged["strike"] <= merged["nikkei_close"]),
        merged["magnet_weight"],
        0,
    )

    result = merged.groupby("date", as_index=False).agg(
        call_magnet=("call_magnet", "sum"),
        put_magnet=("put_magnet", "sum"),
        total_option_magnet=("magnet_weight", "sum"),
    )
    result["option_bias"] = result["call_magnet"] - result["put_magnet"]
    return result


def compute_scores(
    constituents: pd.DataFrame,
    index_daily: pd.DataFrame,
    options: pd.DataFrame,
    sq_calendar: pd.DataFrame,
    cfg: ModelConfig,
    sector_tickers: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    idx = index_daily.copy()
    idx["date"] = pd.to_datetime(idx["date"])
    numeric_cols = [
        "nikkei_close",
        "nikkei_prev_close",
        "topix_close",
        "topix_prev_close",
        "nikkei_atr",
        "futures_close",
        "advancers_ratio",
    ]
    idx[numeric_cols] = idx[numeric_cols].apply(pd.to_numeric, errors="coerce")
    idx["nikkei_return"] = idx["nikkei_close"] / idx["nikkei_prev_close"] - 1
    idx["topix_return"] = idx["topix_close"] / idx["topix_prev_close"] - 1
    idx["nikkei_topix_gap"] = idx["nikkei_return"] - idx["topix_return"]
    idx["basis"] = idx["futures_close"] - idx["nikkei_close"]
    idx["basis_atr"] = idx["basis"] / idx["nikkei_atr"].replace(0, np.nan)

    contribution_daily, ranked = compute_contributions(constituents, cfg)
    option_features = compute_option_features(options, idx)

    sq = sq_calendar.copy()
    sq["sq_date"] = pd.to_datetime(sq["sq_date"])
    sq_distance = business_day_distance(idx["date"], sq["sq_date"])

    daily = (
        idx.merge(contribution_daily, on="date", how="left")
        .merge(option_features, on="date", how="left")
        .merge(sq_distance, on="date", how="left")
    )
    fill_zero = [
        "contribution_trend",
        "contribution_concentration",
        "option_bias",
        "call_magnet",
        "put_magnet",
        "total_option_magnet",
    ]
    daily[fill_zero] = daily[fill_zero].fillna(0)

    # --- data-quality guard ---------------------------------------------------
    # A partially-missing raw input (e.g. current index price or futures basis
    # failed to fetch) otherwise cascades into NaN composite scores and the
    # dashboard publishes "nan%". Record which critical inputs are missing on the
    # latest (displayed) row *before* filling, then substitute neutral values so
    # scores stay finite. The missing list surfaces as a warning banner.
    critical_inputs = {
        "nikkei_close": "現在値",
        "basis_atr": "先物ベーシス",
        "nikkei_topix_gap": "TOPIX乖離",
        "advancers_ratio": "騰落比率",
    }
    _missing = [
        jp
        for col, jp in critical_inputs.items()
        if col not in daily.columns or pd.isna(daily.iloc[-1].get(col, np.nan))
    ]
    if "nikkei_close" in daily.columns:
        daily["nikkei_close"] = daily["nikkei_close"].ffill()
    if "advancers_ratio" in daily.columns:
        daily["advancers_ratio"] = daily["advancers_ratio"].fillna(0.5)
    for _col in ("basis_atr", "nikkei_topix_gap", "nikkei_return"):
        if _col in daily.columns:
            daily[_col] = daily[_col].fillna(0)
    daily["data_degraded_inputs"] = ", ".join(_missing)
    # --------------------------------------------------------------------------

    daily["time_pressure"] = np.maximum(0, 1 - daily["days_to_sq"] / cfg.sq_window_days)
    daily["post_sq_flag"] = np.where(
        daily["days_after_sq"].between(1, cfg.post_sq_days, inclusive="both"),
        1,
        0,
    )
    def rz(series: pd.Series) -> pd.Series:
        return rolling_zscore(series, cfg.zscore_window, cfg.zscore_min_periods)

    daily["breadth_divergence"] = daily["nikkei_return"] - rz(daily["advancers_ratio"])
    daily["index_distortion"] = (
        rz(daily["nikkei_topix_gap"])
        + rz(daily["contribution_concentration"])
        - rz(daily["advancers_ratio"])
    )
    # 先物ベーシスは futures_close が現物のプレースホルダー（fetch_market_data.py参照）で
    # 常に0になる構造的な未接続状態のため、合成スコアからは除外する。basis/basis_atr/
    # basis_score列は先物データを接続した際の参考用に残す。
    daily["basis_score"] = rz(daily["basis_atr"])

    daily["sq_up_pressure"] = (
        0.375 * rz(daily["contribution_trend"])
        + 0.3125 * rz(daily["option_bias"])
        + 0.1875 * rz(daily["index_distortion"])
        + 0.125 * daily["time_pressure"]
    )
    daily["sq_down_risk"] = (
        0.3125 * rz(-daily["contribution_trend"])
        + 0.25 * rz(-daily["option_bias"])
        + 0.25 * rz(daily["index_distortion"])
        + 0.1875 * daily["post_sq_flag"]
    )
    daily["p_up"] = logistic(-0.05 + 0.85 * daily["sq_up_pressure"] - 0.65 * daily["sq_down_risk"])

    # 改修3: 上方向圧力と下方向リスクの乖離が大きい「中立」判定を、より情報量のある
    # ラベルに倒す。乖離幅は sq_up_pressure - sq_down_risk。中立域の中でも下方向が
    # 明確に深ければ DOWN_WATCH、上方向が明確に強ければ UP_WATCH。
    daily["up_down_gap"] = daily["sq_up_pressure"] - daily["sq_down_risk"]
    daily["signal"] = np.select(
        [
            daily["p_up"] >= cfg.up_threshold,
            daily["p_up"] <= cfg.down_threshold,
            daily["up_down_gap"] <= -cfg.asym_gap_threshold,
            daily["up_down_gap"] >= cfg.asym_gap_threshold,
        ],
        ["UP_BIAS", "DOWN_BIAS", "DOWN_WATCH", "UP_WATCH"],
        default="NEUTRAL",
    )

    if sector_tickers:
        sector = compute_sector_exposure(ranked, sector_tickers)
        daily = daily.merge(sector, on="date", how="left")
        daily[["sector_contribution", "sector_abs_share", "sector_weight"]] = daily[
            ["sector_contribution", "sector_abs_share", "sector_weight"]
        ].fillna(0.0)

    return daily.sort_values("date"), ranked.sort_values(["date", "contribution_rank"])


def write_dashboard(
    scores: pd.DataFrame,
    ranked: pd.DataFrame,
    options: pd.DataFrame,
    out_path: Path,
    cfg: ModelConfig,
) -> None:
    latest = scores.sort_values("date").iloc[-1]
    latest_date = latest["date"].strftime("%Y-%m-%d")
    latest_ranked = ranked[ranked["date"] == latest["date"]].head(cfg.top_k)
    history = scores.tail(16).copy()

    def pct(value: float) -> str:
        if pd.isna(value):
            return "-"
        return f"{value * 100:.0f}%"

    def fmt(value: float, digits: int = 2) -> str:
        if pd.isna(value):
            return "-"
        return f"{value:.{digits}f}"

    max_index = history["nikkei_close"].max()
    min_index = history["nikkei_close"].min()

    def scale(values: pd.Series, top: int = 56, bottom: int = 214) -> list[float]:
        lo = values.min()
        hi = values.max()
        if hi == lo:
            return [float((top + bottom) / 2)] * len(values)
        return [float(bottom - (v - lo) / (hi - lo) * (bottom - top)) for v in values]

    xs = np.linspace(48, 735, len(history))
    index_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, scale(history["nikkei_close"])))
    up_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, scale(history["sq_up_pressure"])))
    down_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, scale(history["sq_down_risk"])))

    rows = []
    for _, row in latest_ranked.iterrows():
        value = row["contribution_raw"]
        klass = "pos" if value >= 0 else "neg"
        rows.append(
            f"<tr><td>{row['ticker']} {row['name']}</td>"
            f"<td class=\"{klass}\">{value:+.1f}</td>"
            f"<td>{row['index_weight'] * 100:.2f}%</td>"
            f"<td>{row['momentum'] * 100:+.2f}%</td></tr>"
        )

    top5 = latest_ranked.head(5)[["ticker", "name"]].copy()
    ranked_month = ranked.copy()
    ranked_month["date"] = pd.to_datetime(ranked_month["date"])
    month_start = latest["date"].replace(day=1)
    ranked_month = ranked_month[
        (ranked_month["date"] >= month_start)
        & (ranked_month["date"] <= latest["date"])
        & (ranked_month["ticker"].isin(top5["ticker"]))
    ].sort_values(["ticker", "date"])
    trend_svg = '<text x="40" y="80" fill="#637083" font-size="13">当月推移データなし</text>'
    trend_rows = ""
    if not ranked_month.empty:
        colors = ["#c5323a", "#176f8f", "#5a6fdb", "#d98917", "#18785d"]
        color_map = {ticker: colors[i % len(colors)] for i, ticker in enumerate(top5["ticker"])}
        indexed_frames = []
        for ticker, group in ranked_month.groupby("ticker", sort=False):
            group = group.sort_values("date").copy()
            base = group["close"].iloc[0]
            group["indexed_close"] = np.where(base != 0, group["close"] / base * 100, 100)
            indexed_frames.append(group)
        indexed = pd.concat(indexed_frames, ignore_index=True)
        y_min = min(95, indexed["indexed_close"].min())
        y_max = max(105, indexed["indexed_close"].max())
        if y_max == y_min:
            y_max = y_min + 1
        unique_dates = sorted(indexed["date"].drop_duplicates())
        date_to_x = {
            dt: 58 + i * (720 / max(len(unique_dates) - 1, 1))
            for i, dt in enumerate(unique_dates)
        }

        def y_scale(v: float) -> float:
            return 238 - (v - y_min) / (y_max - y_min) * 172

        lines = []
        markers = []
        legend_items = []
        trend_table_rows = []
        for _, item in top5.iterrows():
            ticker = item["ticker"]
            name = item["name"]
            group = indexed[indexed["ticker"] == ticker].sort_values("date")
            if group.empty:
                continue
            points = " ".join(
                f"{date_to_x[row['date']]:.1f},{y_scale(row['indexed_close']):.1f}"
                for _, row in group.iterrows()
            )
            color = color_map[ticker]
            lines.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.6" points="{points}"/>')
            last = group.iloc[-1]
            markers.append(
                f'<circle cx="{date_to_x[last["date"]]:.1f}" cy="{y_scale(last["indexed_close"]):.1f}" r="3.5" fill="{color}"/>'
            )
            legend_items.append(
                f'<span><i class="dot" style="background:{color};"></i>{ticker} {name}</span>'
            )
            month_return = last["indexed_close"] - 100
            klass = "pos" if month_return >= 0 else "neg"
            trend_table_rows.append(
                f'<tr><td>{ticker} {name}</td><td class="{klass}">{month_return:+.2f}%</td>'
                f'<td>{last["close"]:,.0f}</td><td>{last["contribution_raw"]:+.1f}</td></tr>'
            )
        x_labels = ""
        if unique_dates:
            x_labels = (
                f'<text x="58" y="266" fill="#637083" font-size="11">{unique_dates[0].strftime("%m-%d")}</text>'
                f'<text x="720" y="266" fill="#637083" font-size="11">{unique_dates[-1].strftime("%m-%d")}</text>'
            )
        grid = (
            '<g stroke="#d9e0ea"><line x1="48" y1="66" x2="808" y2="66"/>'
            '<line x1="48" y1="109" x2="808" y2="109"/>'
            '<line x1="48" y1="152" x2="808" y2="152"/>'
            '<line x1="48" y1="195" x2="808" y2="195"/>'
            '<line x1="48" y1="238" x2="808" y2="238"/></g>'
        )
        trend_svg = (
            grid
            + "".join(lines)
            + "".join(markers)
            + f'<text x="48" y="28" fill="#637083" font-size="11">月初=100 / range {y_min:.1f}-{y_max:.1f}</text>'
            + x_labels
        )
        trend_rows = "".join(trend_table_rows)
        trend_legend = "".join(legend_items)
    else:
        trend_legend = ""

    option_latest = options.copy()
    option_latest["date"] = pd.to_datetime(option_latest["date"])
    option_latest = option_latest[option_latest["date"] == latest["date"]].copy()
    option_latest["type"] = option_latest["type"].str.upper().str[0]
    option_pivot = (
        option_latest.pivot_table(index="strike", columns="type", values="open_interest", aggfunc="sum")
        .fillna(0)
        .reset_index()
        .sort_values("strike")
    )
    if option_pivot.empty:
        option_svg = '<text x="40" y="80" fill="#637083" font-size="13">オプション建玉データなし</text>'
    else:
        max_oi = max(option_pivot.get("C", pd.Series([0])).max(), option_pivot.get("P", pd.Series([0])).max(), 1)
        x_positions = np.linspace(70, 790, len(option_pivot))
        bar_width = min(28, max(8, 520 / max(len(option_pivot), 1)))
        bars = []
        labels = []
        for x, (_, row) in zip(x_positions, option_pivot.iterrows()):
            call_h = float(row.get("C", 0)) / max_oi * 150
            put_h = float(row.get("P", 0)) / max_oi * 150
            bars.append(f'<rect x="{x - bar_width:.1f}" y="{250 - put_h:.1f}" width="{bar_width:.1f}" height="{put_h:.1f}" fill="#176f8f"/>')
            bars.append(f'<rect x="{x:.1f}" y="{250 - call_h:.1f}" width="{bar_width:.1f}" height="{call_h:.1f}" fill="#c5323a"/>')
            if len(option_pivot) <= 14 or int(row["strike"]) % 1000 == 0:
                labels.append(f'<text x="{x - 18:.1f}" y="272" fill="#637083" font-size="10">{int(row["strike"] / 1000)}k</text>')
        strike_min = option_pivot["strike"].min()
        strike_max = option_pivot["strike"].max()
        current_x = 70 + (latest["nikkei_close"] - strike_min) / max(strike_max - strike_min, 1) * 720
        current_x = min(820, max(40, current_x))
        option_svg = (
            '<g stroke="#d9e0ea"><line x1="50" y1="250" x2="820" y2="250"/>'
            '<line x1="50" y1="60" x2="50" y2="250"/></g>'
            + "".join(bars)
            + f'<line x1="{current_x:.1f}" y1="48" x2="{current_x:.1f}" y2="258" stroke="#172033" stroke-width="2" stroke-dasharray="5 4"/>'
            + f'<text x="{min(current_x + 10, 710):.1f}" y="64" fill="#172033" font-size="12">Nikkei {latest["nikkei_close"]:,.0f}</text>'
            + "".join(labels)
        )

    meter_up = min(100, max(0, (latest["sq_up_pressure"] + 2) / 4 * 100))
    meter_down = min(100, max(0, (latest["sq_down_risk"] + 2) / 4 * 100))
    label = {
        "UP_BIAS": "上方向優位",
        "DOWN_BIAS": "下方向優位",
        "DOWN_WATCH": "下方向警戒",
        "UP_WATCH": "上方向警戒",
        "NEUTRAL": "中立",
    }.get(str(latest["signal"]), "中立")

    sector_html = ""
    if "sector_abs_share" in latest.index:
        share = float(latest["sector_abs_share"])
        weight = float(latest.get("sector_weight", 0.0))
        contrib = float(latest.get("sector_contribution", 0.0))
        if share >= cfg.sector_abs_share_alert:
            sector_color, sector_tag = "var(--up)", "強い連動"
        elif share >= cfg.sector_abs_share_watch:
            sector_color, sector_tag = "var(--warn)", "連動注意"
        else:
            sector_color, sector_tag = "var(--good)", "限定的"
        sector_html = f"""
      <section class="span-3"><h2>半導体バスケット連動度</h2><div class="metric"><strong style="color:{sector_color}">{pct(share)}</strong><small>当日|寄与|シェア</small></div><div class="meter" style="--value:{min(100, share * 100):.0f}%"><div style="background:{sector_color};"></div></div><div class="note">寄与合計 {contrib:+.1f} / 追跡銘柄内ウェイト {pct(weight)}。{sector_tag}。個別で回避していても指数β経由で取り込む量の可視化（売買判断ではない）。</div></section>"""
    metadata_path = out_path.parent / "data_source_metadata.json"
    source_notice = ""
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            source_notice = (
                f"価格: {metadata.get('price_source', '-')} / "
                f"構成銘柄: {metadata.get('constituent_mode', '-')} / "
                f"オプション建玉: {metadata.get('option_oi_source', '-')}"
            )
            if metadata.get("option_oi_is_proxy"):
                source_notice += " / 注意: オプション建玉は暫定プロキシ"
        except json.JSONDecodeError:
            source_notice = ""

    degraded = str(latest.get("data_degraded_inputs", "") or "").strip()
    if degraded:
        warn = (
            f"⚠ データ取得が不完全です（欠損: {degraded}）。"
            "該当指標は中立値で代替した参考値です。売買判断には使わないでください。"
        )
        source_notice = f"{source_notice} / {warn}" if source_notice else warn

    html = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>日経225 SQ歪みダッシュボード</title>
  <style>
    :root {{ --ink:#172033; --muted:#637083; --line:#d9e0ea; --panel:#fff; --bg:#f4f7fb; --up:#c5323a; --down:#176f8f; --accent:#5a6fdb; --warn:#d98917; --good:#18785d; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family:"Segoe UI","Yu Gothic UI","Meiryo",sans-serif; background:var(--bg); color:var(--ink); }}
    header {{ padding:20px 28px 12px; border-bottom:1px solid var(--line); background:#fff; }}
    h1 {{ margin:0; font-size:22px; letter-spacing:0; }}
    .sub {{ margin-top:6px; color:var(--muted); font-size:13px; }}
    main {{ padding:20px 28px 32px; display:grid; gap:18px; }}
    .grid {{ display:grid; grid-template-columns:repeat(12,1fr); gap:18px; }}
    section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; min-width:0; }}
    h2 {{ margin:0 0 12px; font-size:14px; }}
    .span-3 {{ grid-column:span 3; }} .span-5 {{ grid-column:span 5; }} .span-7 {{ grid-column:span 7; }} .span-12 {{ grid-column:span 12; }}
    .metric {{ display:flex; align-items:baseline; gap:8px; }} .metric strong {{ font-size:28px; line-height:1; }} .metric small,.note {{ color:var(--muted); font-size:12px; }}
    .note {{ margin-top:10px; line-height:1.5; }}
    .badge {{ display:inline-flex; align-items:center; min-height:28px; padding:0 10px; border-radius:999px; font-size:12px; font-weight:700; background:#fff2d8; color:#7a4a00; border:1px solid #f2c66f; }}
    .notice {{ margin-top:10px; padding:10px 12px; border:1px solid #f2c66f; background:#fff8e8; color:#6e4b00; border-radius:8px; font-size:12px; line-height:1.5; }}
    .meter {{ height:12px; border-radius:999px; overflow:hidden; background:#e9eef5; margin-top:14px; }} .meter div {{ height:100%; width:var(--value); background:linear-gradient(90deg,var(--accent),var(--up)); }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }} th,td {{ padding:8px 6px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }} th:first-child,td:first-child {{ text-align:left; }} th {{ color:var(--muted); font-weight:600; }}
    .pos {{ color:var(--up); font-weight:700; }} .neg {{ color:var(--down); font-weight:700; }}
    .legend {{ display:flex; gap:14px; flex-wrap:wrap; color:var(--muted); font-size:12px; margin-bottom:8px; }} .dot {{ width:9px; height:9px; display:inline-block; border-radius:50%; margin-right:5px; }}
    svg {{ width:100%; height:auto; display:block; }}
    @media (max-width: 980px) {{ .span-3,.span-5,.span-7 {{ grid-column:span 12; }} header,main {{ padding-left:16px; padding-right:16px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>日経225 SQ歪みダッシュボード</h1>
    <div class="sub">基準日: {latest_date} / 次回SQ: {latest['next_sq_date'].strftime('%Y-%m-%d')} / 入力データから自動生成</div>
    {f'<div class="notice">{source_notice}</div>' if source_notice else ''}
  </header>
  <main>
    <div class="grid">
      <section class="span-3"><h2>上方向SQ圧力</h2><div class="metric"><strong class="pos">{fmt(latest['sq_up_pressure'])}</strong><small>z-like(直近{cfg.zscore_window}日窓)</small></div><div class="meter" style="--value:{meter_up:.0f}%"><div></div></div><div class="note">寄与度トレンド、オプションバイアス、指数歪み、SQ接近度の合成（先物ベーシスは未接続のため除外）。</div></section>
      <section class="span-3"><h2>下方向剥落リスク</h2><div class="metric"><strong class="neg">{fmt(latest['sq_down_risk'])}</strong><small>z-like(直近{cfg.zscore_window}日窓)</small></div><div class="meter" style="--value:{meter_down:.0f}%"><div style="background:linear-gradient(90deg,var(--down),var(--warn));"></div></div><div class="note">SQ後フラグ、逆方向寄与度、指数歪みの合成（先物ベーシスは未接続のため除外）。</div></section>
      <section class="span-3"><h2>推定勝率</h2><div class="metric"><strong>{pct(latest['p_up'])}</strong><small>翌日上昇</small></div><div class="meter" style="--value:{latest['p_up'] * 100:.0f}%"><div style="background:linear-gradient(90deg,var(--good),var(--accent));"></div></div><div class="note">閾値: 上方向 {cfg.up_threshold:.0%} / 下方向 {cfg.down_threshold:.0%}</div></section>
      <section class="span-3"><h2>判定</h2><span class="badge">{label}</span><div class="note">SQまで {int(latest['days_to_sq'])} 営業日。TimePressure={fmt(latest['time_pressure'])}</div></section>{sector_html}
      <section class="span-7">
        <h2>指数と歪みスコア</h2>
        <div class="legend"><span><i class="dot" style="background:var(--ink);"></i>日経225</span><span><i class="dot" style="background:var(--up);"></i>SQ上方向圧力</span><span><i class="dot" style="background:var(--down);"></i>下方向リスク</span></div>
        <svg viewBox="0 0 760 280" role="img" aria-label="指数と歪みスコアの推移">
          <rect width="760" height="280" fill="#fff"/>
          <g stroke="#d9e0ea"><line x1="42" y1="30" x2="735" y2="30"/><line x1="42" y1="85" x2="735" y2="85"/><line x1="42" y1="140" x2="735" y2="140"/><line x1="42" y1="195" x2="735" y2="195"/><line x1="42" y1="250" x2="735" y2="250"/></g>
          <polyline fill="none" stroke="#172033" stroke-width="3" points="{index_points}"/>
          <polyline fill="none" stroke="#c5323a" stroke-width="3" points="{up_points}"/>
          <polyline fill="none" stroke="#176f8f" stroke-width="3" points="{down_points}"/>
          <text x="48" y="270" fill="#637083" font-size="11">{history.iloc[0]['date'].strftime('%m-%d')}</text>
          <text x="680" y="270" fill="#637083" font-size="11">{latest_date[5:]}</text>
          <text x="48" y="22" fill="#637083" font-size="11">Nikkei range {min_index:,.0f}-{max_index:,.0f}</text>
        </svg>
      </section>
      <section class="span-5">
        <h2>当日寄与度上位</h2>
        <table><thead><tr><th>銘柄</th><th>寄与</th><th>指数W</th><th>{cfg.momentum_days}日M</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
      </section>
      <section class="span-12">
        <h2>当日寄与度上位5銘柄 当月推移</h2>
        <div class="legend">{trend_legend}</div>
        <svg viewBox="0 0 860 288" role="img" aria-label="当日寄与度上位5銘柄の当月株価推移">
          <rect width="860" height="288" fill="#fff"/>
          {trend_svg}
        </svg>
        <table><thead><tr><th>銘柄</th><th>月初来</th><th>終値</th><th>当日寄与</th></tr></thead><tbody>{trend_rows}</tbody></table>
      </section>
      <section class="span-12">
        <h2>オプション建玉ストライク分布</h2>
        <div class="legend"><span><i class="dot" style="background:var(--up);"></i>Call OI</span><span><i class="dot" style="background:var(--down);"></i>Put OI</span><span>縦線: 現在値</span></div>
        <svg viewBox="0 0 860 300" role="img" aria-label="オプション建玉分布">
          <rect width="860" height="300" fill="#fff"/>
          {option_svg}
        </svg>
      </section>
      <section class="span-12">
        <h2>歪み内訳</h2>
        <table><thead><tr><th>指標</th><th>値</th><th>説明</th></tr></thead><tbody>
          <tr><td>寄与度トレンド</td><td>{fmt(latest['contribution_trend'], 4)}</td><td>高寄与度銘柄の指数ウェイト付きモメンタム</td></tr>
          <tr><td>オプションバイアス</td><td>{fmt(latest['option_bias'])}</td><td>上方Call吸引と下方Put吸引の差</td></tr>
          <tr><td>先物ベーシス（参考・未使用）</td><td>{fmt(latest['basis'])}</td><td>日経225先物 - 現物指数。先物データ未接続のため常に0。合成スコアからは除外済み</td></tr>
          <tr><td>乖離（上方向-下方向）</td><td>{fmt(latest['up_down_gap'])}</td><td>{cfg.asym_gap_threshold:.2f}以上/以下で中立から警戒ラベルへ</td></tr>
          <tr><td>指数歪み</td><td>{fmt(latest['index_distortion'])}</td><td>日経平均との差、寄与度集中、騰落比率の合成</td></tr>
          <tr><td>寄与度集中</td><td>{pct(latest['contribution_concentration'])}</td><td>上位{cfg.top_k}銘柄の絶対寄与度シェア</td></tr>
        </tbody></table>
      </section>
    </div>
  </main>
</body>
</html>
"""
    out_path.write_text(html, encoding="utf-8")


def generate_sample_data(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2026-05-21", periods=17)
    tickers = [
        ("285A", "キオクシア", 0.82, 10400),
        ("5803", "フジクラ", 0.70, 14200),
        ("6857", "アドバンテスト", 2.10, 12800),
        ("9984", "ソフトバンクG", 3.00, 16200),
        ("8035", "東京エレクトロン", 1.40, 31500),
        ("9983", "ファーストリテイリング", 2.70, 51000),
        ("6920", "レーザーテック", 0.90, 24500),
        ("7203", "トヨタ自動車", 1.00, 3200),
        ("6098", "リクルート", 1.00, 9800),
        ("4063", "信越化学", 1.00, 6500),
        ("4519", "中外製薬", 1.00, 7100),
        ("9433", "KDDI", 1.00, 4950),
    ]

    rng = np.random.default_rng(225)
    rows = []
    price_state = {ticker: price for ticker, _, _, price in tickers}
    for day_idx, date in enumerate(dates):
        sq_push = max(0, day_idx - 11) * 0.006
        for ticker, name, adj, _ in tickers:
            prev = price_state[ticker]
            leader = ticker in {"285A", "5803", "6857", "9984"}
            drift = (0.004 + sq_push) if leader else 0.001
            shock = rng.normal(0, 0.012)
            close = max(100, prev * (1 + drift + shock))
            rows.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "ticker": ticker,
                    "name": name,
                    "close": round(close, 2),
                    "prev_close": round(prev, 2),
                    "adj_factor": adj,
                    "volume": int(rng.integers(700_000, 8_000_000)),
                }
            )
            price_state[ticker] = close
    pd.DataFrame(rows).to_csv(out_dir / "constituents_daily.csv", index=False, encoding="utf-8")

    index_rows = []
    nikkei = 60500.0
    topix = 3250.0
    for day_idx, date in enumerate(dates):
        prev_n = nikkei
        prev_t = topix
        sq_push = max(0, day_idx - 11) * 115
        nikkei = nikkei + 120 + sq_push + rng.normal(0, 160)
        topix = topix + 3 + max(0, day_idx - 11) * 1.3 + rng.normal(0, 8)
        index_rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "nikkei_close": round(nikkei, 2),
                "nikkei_prev_close": round(prev_n, 2),
                "topix_close": round(topix, 2),
                "topix_prev_close": round(prev_t, 2),
                "nikkei_atr": 520,
                "futures_close": round(nikkei + 30 + max(0, day_idx - 11) * 22, 2),
                "advancers_ratio": round(max(0.28, min(0.74, 0.55 - max(0, day_idx - 11) * 0.035 + rng.normal(0, 0.05))), 3),
            }
        )
    pd.DataFrame(index_rows).to_csv(out_dir / "index_daily.csv", index=False, encoding="utf-8")

    option_rows = []
    expiry = "2026-06-12"
    for date in dates:
        for strike in range(59500, 67501, 500):
            call_base = 2500 + max(0, strike - 62000) / 500 * 420
            put_base = 3000 + max(0, 62000 - strike) / 500 * 360
            option_rows.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "expiry": expiry,
                    "type": "C",
                    "strike": strike,
                    "open_interest": int(call_base + rng.integers(0, 1200)),
                }
            )
            option_rows.append(
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "expiry": expiry,
                    "type": "P",
                    "strike": strike,
                    "open_interest": int(put_base + rng.integers(0, 1000)),
                }
            )
    pd.DataFrame(option_rows).to_csv(out_dir / "options_oi.csv", index=False, encoding="utf-8")
    pd.DataFrame(
        [
            {"sq_date": "2026-06-12", "kind": "major"},
            {"sq_date": "2026-09-11", "kind": "major"},
            {"sq_date": "2026-12-11", "kind": "major"},
        ]
    ).to_csv(out_dir / "sq_calendar.csv", index=False, encoding="utf-8")


HISTORY_COLUMNS = [
    "date",
    "nikkei_close",
    "sq_up_pressure",
    "sq_down_risk",
    "up_down_gap",
    "p_up",
    "signal",
    "days_to_sq",
    "days_after_sq",
    "sector_abs_share",
]


def append_score_history(scores: pd.DataFrame, history_path: Path, cfg: ModelConfig) -> None:
    """Append today's computed rows to a persisted, git-tracked archive.

    Append-only and immutable: a date already present in the file is never
    rewritten, even if this run recomputes a different value for it (a later
    code/logic change would otherwise silently rewrite history on every run,
    reintroducing the same "past judgment changes over time" problem that
    switching to a rolling z-score was meant to fix). Same-day reruns are
    skipped, not merged. zscore_window/basis_included are stamped per row so
    a schema/logic change is visible in the data rather than blended in
    silently.
    """
    cols = [c for c in HISTORY_COLUMNS if c in scores.columns]
    new_rows = scores[cols].copy()
    new_rows["date"] = pd.to_datetime(new_rows["date"]).dt.strftime("%Y-%m-%d")
    new_rows["zscore_window"] = cfg.zscore_window
    # basisは futures_close が現物のプレースホルダーで常に0の構造的未接続状態のため、
    # 現行ロジックでは合成スコアから除外している（sq_distortion_model.py compute_scores参照）。
    new_rows["basis_included"] = False

    if history_path.exists():
        existing = pd.read_csv(history_path)
        existing["date"] = pd.to_datetime(existing["date"]).dt.strftime("%Y-%m-%d")
        known_dates = set(existing["date"])
        appended = new_rows[~new_rows["date"].isin(known_dates)]
        combined = pd.concat([existing, appended], ignore_index=True)
    else:
        combined = new_rows
    combined = combined.sort_values("date")
    history_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(history_path, index=False, encoding="utf-8")


def run_pipeline(
    input_dir: Path,
    output_dir: Path,
    cfg: ModelConfig,
    sector_config_path: Path | None = None,
    history_path: Path | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    constituents = read_csv(input_dir / "constituents_daily.csv", REQUIRED_CONSTITUENT_COLUMNS, "constituents_daily.csv")
    index_daily = read_csv(input_dir / "index_daily.csv", REQUIRED_INDEX_COLUMNS, "index_daily.csv")
    options = read_csv(input_dir / "options_oi.csv", REQUIRED_OPTION_COLUMNS, "options_oi.csv")
    sq_calendar = read_csv(input_dir / "sq_calendar.csv", REQUIRED_SQ_COLUMNS, "sq_calendar.csv")

    sector_baskets = load_sector_baskets(sector_config_path)
    sector_tickers = sector_baskets.get("semiconductor", {}).get("tickers")

    scores, ranked = compute_scores(constituents, index_daily, options, sq_calendar, cfg, sector_tickers)
    if history_path is not None:
        append_score_history(scores, history_path, cfg)
    scores.to_csv(output_dir / "sq_scores.csv", index=False, encoding="utf-8")
    ranked.to_csv(output_dir / "sq_contributions.csv", index=False, encoding="utf-8")
    write_dashboard(scores, ranked, options, output_dir / "sq_distortion_dashboard_generated.html", cfg)
    latest_signal = scores.sort_values("date").iloc[-1].replace({np.nan: None}).to_dict()
    (output_dir / "sq_latest_signal.json").write_text(
        json.dumps(
            latest_signal,
            ensure_ascii=False,
            allow_nan=False,
            default=str,
            indent=2,
        ),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute Nikkei 225 SQ distortion scores.")
    parser.add_argument("--input-dir", type=Path, default=Path("work/sample_data"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--generate-sample", action="store_true", help="Generate sample CSV inputs before scoring.")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--momentum-days", type=int, default=5)
    parser.add_argument("--sector-config", type=Path, default=Path("work/config/sector_baskets.json"))
    parser.add_argument(
        "--history-path",
        type=Path,
        default=None,
        help="Append daily scores to this CSV (e.g. data/sq_score_history.csv). Off by default for local/sample runs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = ModelConfig(top_k=args.top_k, momentum_days=args.momentum_days)
    if args.generate_sample:
        generate_sample_data(args.input_dir)
    run_pipeline(args.input_dir, args.output_dir, cfg, args.sector_config, args.history_path)


if __name__ == "__main__":
    main()
