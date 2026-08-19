"""Regime-transition 6-condition panel (kabu cockpit v3 追加⑥).

Fetches the four automatable conditions from FRED (no API key required for
its CSV endpoint) and Yahoo Finance, merges in the two manually-maintained
conditions, and writes a single JSON that cockpit.html reads with fetch().

Data hygiene (existing repo convention, see data/README.md):
- On fetch failure, an indicator keeps its last known good value instead of
  going NaN. That "last known good" is persisted server-side in
  data/regime_transition_state.json (git-tracked) — the browser is never
  asked to remember anything, and multiple viewers/devices stay consistent.
- Each indicator carries its own value/as_of/status, so a partial failure
  (e.g. FRED down but yfinance fine) is visible per-condition, not as one
  blanket "data stale" flag.
- This script only ever writes outputs/regime_transition.json and
  data/regime_transition_state.json. It never touches index.html, cockpit.html,
  or sq.html.

条件の判定方法（設計意図: 水準ではなく組み合わせと速度）:

1. 米10年金利が速いスピードで5%を明確に上回る
   -> DGS10 最新値 > 5.00 かつ 直近20営業日の変化幅 >= +30bp
   （20d変化幅の90パーセンタイル近辺を「速い」の目安とした）
3. クレジットスプレッド拡大
   -> HY OAS (BAMLH0A0HYM2) の直近20営業日の変化幅 >= +30bp
5. ドルとクレジットが同時に崩れる
   -> DXY と HY OAS の直近10営業日の変化が両方ともプラス（同時に悪化方向）
6. 超長期金利が一日10bp単位で連続上昇
   -> DGS30 の日次変化が2営業日連続で+10bp以上（直近5営業日以内に発生）

条件2・4は自動取得不可のため data/regime_manual_inputs.json を人が編集する。
未入力は status="unevaluated" とし、点灯数の分母（評価可能条件数）から除外する。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

FRED_10Y = "DGS10"
FRED_30Y = "DGS30"
FRED_HY_OAS = "BAMLH0A0HYM2"
DXY_SYMBOL = "DX-Y.NYB"

CONDITION_LABELS = {
    "1": "米10年金利が速いスピードで5%を明確に上回る",
    "2": "企業利益予想が下方修正される（S&P500 forward EPS改定率）",
    "3": "クレジットスプレッドが拡大する（HY OAS）",
    "4": "AI投資の収益性・生産性期待が後退する（ハイパースケーラーCapEx）",
    "5": "ドルやクレジット市場まで同時に崩れる（DXY・HY OAS同時方向）",
    "6": "超長期金利が一日10bp単位で連続上昇する（米30年）",
}


def fetch_fred_series(series_id: str) -> pd.DataFrame:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    df = pd.read_csv(url)
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"]).sort_values("date").reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"FRED series {series_id} returned no usable rows")
    return df


def fetch_yf_series(symbol: str, period: str = "6mo") -> pd.DataFrame:
    raw = yf.download(symbol, period=period, progress=False, threads=False)
    if raw.empty:
        raise RuntimeError(f"Yahoo Finance returned no data for {symbol}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [c[0] for c in raw.columns]
    df = raw.reset_index()[["Date", "Close"]].rename(columns={"Date": "date", "Close": "value"})
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["value"]).sort_values("date").reset_index(drop=True)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def carry_forward(condition_id: str, state: dict, attempted_at: str, error: str) -> dict:
    prior = state.get(condition_id)
    if not prior or prior.get("status") == "unevaluated":
        return {
            "label": CONDITION_LABELS[condition_id],
            "lit": None,
            "status": "unevaluated",
            "value": None,
            "as_of": None,
            "detail": f"データ取得失敗（{attempted_at}）: {error}。過去の正常値もありません。",
        }
    return {
        "label": CONDITION_LABELS[condition_id],
        "lit": prior.get("lit"),
        "status": "stale",
        "value": prior.get("value"),
        "as_of": prior.get("as_of"),
        "detail": f"データ取得失敗：前回値を表示中（前回as_of={prior.get('as_of')} / 取得試行={attempted_at}）: {error}",
    }


def condition_1(state: dict, attempted_at: str) -> dict:
    try:
        dgs10 = fetch_fred_series(FRED_10Y)
        latest = dgs10.iloc[-1]
        change_20d_bp = None
        if len(dgs10) > 20:
            change_20d_bp = float((dgs10["value"].iloc[-1] - dgs10["value"].iloc[-21]) * 100)
        lit = bool(latest["value"] > 5.0 and (change_20d_bp or 0) >= 30)
        return {
            "label": CONDITION_LABELS["1"],
            "lit": lit,
            "status": "ok",
            "value": round(float(latest["value"]), 2),
            "as_of": latest["date"].strftime("%Y-%m-%d"),
            "detail": f"DGS10={latest['value']:.2f}% / 20営業日変化={change_20d_bp:+.0f}bp" if change_20d_bp is not None else f"DGS10={latest['value']:.2f}%",
        }
    except Exception as exc:  # noqa: BLE001 - fall back to last known good
        return carry_forward("1", state, attempted_at, str(exc))


def condition_3(state: dict, attempted_at: str) -> dict:
    try:
        hy = fetch_fred_series(FRED_HY_OAS)
        latest = hy.iloc[-1]
        change_20d_bp = None
        if len(hy) > 20:
            change_20d_bp = float((hy["value"].iloc[-1] - hy["value"].iloc[-21]) * 100)
        lit = bool((change_20d_bp or 0) >= 30)
        return {
            "label": CONDITION_LABELS["3"],
            "lit": lit,
            "status": "ok",
            "value": round(float(latest["value"]), 2),
            "as_of": latest["date"].strftime("%Y-%m-%d"),
            "detail": f"HY OAS={latest['value']:.2f}% / 20営業日変化={change_20d_bp:+.0f}bp" if change_20d_bp is not None else f"HY OAS={latest['value']:.2f}%",
        }
    except Exception as exc:  # noqa: BLE001
        return carry_forward("3", state, attempted_at, str(exc))


def condition_5(state: dict, attempted_at: str) -> dict:
    try:
        dxy = fetch_yf_series(DXY_SYMBOL)
        hy = fetch_fred_series(FRED_HY_OAS)
        dxy_chg_10d = float(dxy["value"].iloc[-1] - dxy["value"].iloc[-11]) if len(dxy) > 10 else None
        hy_chg_10d_bp = float((hy["value"].iloc[-1] - hy["value"].iloc[-11]) * 100) if len(hy) > 10 else None
        lit = bool((dxy_chg_10d or 0) > 0 and (hy_chg_10d_bp or 0) > 0)
        as_of = min(dxy["date"].iloc[-1], hy["date"].iloc[-1]).strftime("%Y-%m-%d")
        return {
            "label": CONDITION_LABELS["5"],
            "lit": lit,
            "status": "ok",
            "value": {"dxy_chg_10d": dxy_chg_10d, "hy_oas_chg_10d_bp": hy_chg_10d_bp},
            "as_of": as_of,
            "detail": f"DXY 10日変化={dxy_chg_10d:+.2f} / HY OAS 10日変化={hy_chg_10d_bp:+.0f}bp（両方プラスで点灯）"
            if dxy_chg_10d is not None and hy_chg_10d_bp is not None
            else "データ不足",
        }
    except Exception as exc:  # noqa: BLE001
        return carry_forward("5", state, attempted_at, str(exc))


def condition_6(state: dict, attempted_at: str) -> dict:
    try:
        dgs30 = fetch_fred_series(FRED_30Y)
        recent = dgs30.tail(6).copy()
        recent["chg_bp"] = recent["value"].diff() * 100
        streak = (recent["chg_bp"] >= 10) & (recent["chg_bp"].shift(1) >= 10)
        lit = bool(streak.fillna(False).any())
        latest = dgs30.iloc[-1]
        last_chg_bp = float(recent["chg_bp"].iloc[-1]) if len(recent) > 1 else None
        return {
            "label": CONDITION_LABELS["6"],
            "lit": lit,
            "status": "ok",
            "value": round(float(latest["value"]), 2),
            "as_of": latest["date"].strftime("%Y-%m-%d"),
            "detail": f"DGS30={latest['value']:.2f}% / 直近日次変化={last_chg_bp:+.0f}bp、直近5営業日以内に2日連続+10bp以上があれば点灯"
            if last_chg_bp is not None
            else f"DGS30={latest['value']:.2f}%",
        }
    except Exception as exc:  # noqa: BLE001
        return carry_forward("6", state, attempted_at, str(exc))


def load_manual_conditions(path: Path) -> dict:
    manual = load_json(path)
    out = {}
    for cid in ("2", "4"):
        entry = manual.get(f"condition_{cid}", {})
        lit = entry.get("lit")
        status = "unevaluated" if lit is None else "ok"
        out[cid] = {
            "label": CONDITION_LABELS[cid],
            "lit": lit,
            "status": status,
            "value": entry.get("value"),
            "as_of": entry.get("as_of"),
            "detail": entry.get("note") or ("手動未入力" if status == "unevaluated" else "手動入力"),
        }
    return out


def build_panel(
    state_path: Path,
    manual_path: Path,
) -> tuple[dict, dict]:
    state = load_json(state_path)
    attempted_at = pd.Timestamp.now("UTC").isoformat()

    auto = {
        "1": condition_1(state, attempted_at),
        "3": condition_3(state, attempted_at),
        "5": condition_5(state, attempted_at),
        "6": condition_6(state, attempted_at),
    }
    manual = load_manual_conditions(manual_path)

    conditions = {**auto, **manual}
    ordered = {k: conditions[k] for k in ["1", "2", "3", "4", "5", "6"]}

    evaluated = [c for c in ordered.values() if c["status"] != "unevaluated"]
    lit_count = sum(1 for c in evaluated if c["lit"])
    evaluated_count = len(evaluated)

    if lit_count <= 1:
        gauge_label = "結果としての高金利"
    elif lit_count <= 3:
        gauge_label = "警戒"
    else:
        gauge_label = "原因としての高金利"

    panel = {
        "generated_at": attempted_at,
        "conditions": ordered,
        "lit_count": lit_count,
        "evaluated_count": evaluated_count,
        "total_conditions": 6,
        "gauge_label": gauge_label,
    }
    # Persist only the auto-fetched conditions as next run's last-known-good.
    return panel, auto


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the regime-transition 6-condition panel JSON.")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--state-path", type=Path, default=Path("data/regime_transition_state.json"))
    parser.add_argument("--manual-path", type=Path, default=Path("data/regime_manual_inputs.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    panel, auto_state = build_panel(args.state_path, args.manual_path)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "regime_transition.json").write_text(
        json.dumps(panel, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    # 自動取得4条件（1,3,5,6）のみを保存する。手動入力（条件2,4）はここに混ぜない
    # ——data/regime_manual_inputs.json はユーザーが手で編集する別ファイルで、
    # このスクリプトは読むだけで一切書き換えない。
    state_out = {
        "_note": "CI (work/regime_transition.py) が書き換える last-known-good アーカイブ。"
        "自動取得4条件（1,3,5,6）のみ。手動入力（条件2,4）は data/regime_manual_inputs.json 側にあり、"
        "このファイルには含まれない。",
        **auto_state,
    }
    args.state_path.parent.mkdir(parents=True, exist_ok=True)
    args.state_path.write_text(
        json.dumps(state_out, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
