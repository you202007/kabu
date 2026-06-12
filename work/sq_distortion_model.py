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


def zscore(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    std = values.std(ddof=0)
    if not np.isfinite(std) or std == 0:
        return pd.Series(np.zeros(len(values)), index=series.index)
    return (values - values.mean()) / std


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
        days_to_sq = sum(1 for d in known if current < d <= next_sq)
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

    daily["time_pressure"] = np.maximum(0, 1 - daily["days_to_sq"] / cfg.sq_window_days)
    daily["post_sq_flag"] = np.where(
        daily["days_after_sq"].between(1, cfg.post_sq_days, inclusive="both"),
        1,
        0,
    )
    daily["breadth_divergence"] = daily["nikkei_return"] - zscore(daily["advancers_ratio"])
    daily["index_distortion"] = (
        zscore(daily["nikkei_topix_gap"])
        + zscore(daily["contribution_concentration"])
        - zscore(daily["advancers_ratio"])
    )
    daily["basis_score"] = zscore(daily["basis_atr"])

    daily["sq_up_pressure"] = (
        0.30 * zscore(daily["contribution_trend"])
        + 0.25 * zscore(daily["option_bias"])
        + 0.20 * daily["basis_score"]
        + 0.15 * zscore(daily["index_distortion"])
        + 0.10 * daily["time_pressure"]
    )
    daily["sq_down_risk"] = (
        0.25 * zscore(-daily["contribution_trend"])
        + 0.20 * zscore(-daily["option_bias"])
        + 0.20 * zscore(-daily["basis_score"])
        + 0.20 * zscore(daily["index_distortion"])
        + 0.15 * daily["post_sq_flag"]
    )
    daily["p_up"] = logistic(-0.05 + 0.85 * daily["sq_up_pressure"] - 0.65 * daily["sq_down_risk"])
    daily["signal"] = np.select(
        [daily["p_up"] >= cfg.up_threshold, daily["p_up"] <= cfg.down_threshold],
        ["UP_BIAS", "DOWN_BIAS"],
        default="NEUTRAL",
    )

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
        "NEUTRAL": "中立",
    }.get(str(latest["signal"]), "中立")
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
      <section class="span-3"><h2>上方向SQ圧力</h2><div class="metric"><strong class="pos">{fmt(latest['sq_up_pressure'])}</strong><small>z-like</small></div><div class="meter" style="--value:{meter_up:.0f}%"><div></div></div><div class="note">寄与度トレンド、オプションバイアス、先物ベーシス、SQ接近度の合成。</div></section>
      <section class="span-3"><h2>下方向剥落リスク</h2><div class="metric"><strong class="neg">{fmt(latest['sq_down_risk'])}</strong><small>z-like</small></div><div class="meter" style="--value:{meter_down:.0f}%"><div style="background:linear-gradient(90deg,var(--down),var(--warn));"></div></div><div class="note">SQ後フラグ、逆方向寄与度、ベーシス悪化、指数歪みの合成。</div></section>
      <section class="span-3"><h2>推定勝率</h2><div class="metric"><strong>{pct(latest['p_up'])}</strong><small>翌日上昇</small></div><div class="meter" style="--value:{latest['p_up'] * 100:.0f}%"><div style="background:linear-gradient(90deg,var(--good),var(--accent));"></div></div><div class="note">閾値: 上方向 {cfg.up_threshold:.0%} / 下方向 {cfg.down_threshold:.0%}</div></section>
      <section class="span-3"><h2>判定</h2><span class="badge">{label}</span><div class="note">SQまで {int(latest['days_to_sq'])} 営業日。TimePressure={fmt(latest['time_pressure'])}</div></section>
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
          <tr><td>先物ベーシス</td><td>{fmt(latest['basis'])}</td><td>日経225先物 - 現物指数</td></tr>
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


def run_pipeline(input_dir: Path, output_dir: Path, cfg: ModelConfig) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    constituents = read_csv(input_dir / "constituents_daily.csv", REQUIRED_CONSTITUENT_COLUMNS, "constituents_daily.csv")
    index_daily = read_csv(input_dir / "index_daily.csv", REQUIRED_INDEX_COLUMNS, "index_daily.csv")
    options = read_csv(input_dir / "options_oi.csv", REQUIRED_OPTION_COLUMNS, "options_oi.csv")
    sq_calendar = read_csv(input_dir / "sq_calendar.csv", REQUIRED_SQ_COLUMNS, "sq_calendar.csv")

    scores, ranked = compute_scores(constituents, index_daily, options, sq_calendar, cfg)
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = ModelConfig(top_k=args.top_k, momentum_days=args.momentum_days)
    if args.generate_sample:
        generate_sample_data(args.input_dir)
    run_pipeline(args.input_dir, args.output_dir, cfg)


if __name__ == "__main__":
    main()
