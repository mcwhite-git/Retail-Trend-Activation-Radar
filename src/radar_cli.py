#!/usr/bin/env python3
"""
Retail Trend & Activation Radar (CLI)

Generates:
- data/trends_signals.csv  (tidy signals with MA, YoY, z-score)
- reports/activation_radar.xlsx  (Signals + Activation_Radar + Top_Months)
- assets/trends_preview.png, assets/activation_radar.png

Usage:
  python -m src.radar_cli --geo US --keywords sneakers,laptops,furniture,cosmetics,groceries --timeframe "today 5-y"

Notes:
- Uses pytrends. Be mindful of rate limits; a small sleep is included.
- Writes a NEW Excel file by default (no append).
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

sns.set_context("talk")
sns.set_style("whitegrid")

# --------- Trends ingestion ----------
def fetch_trends(kw_list: List[str], geo: str, timeframe: str, sleep: float = 1.5) -> pd.DataFrame:
    try:
        from pytrends.request import TrendReq
    except ImportError:
        print("ERROR: pytrends not installed. Run: pip install pytrends", file=sys.stderr)
        sys.exit(1)

    pytrends = TrendReq(hl="en-US", tz=360)
    frames = []
    for i in range(0, len(kw_list), 5):  # pytrends supports up to 5 per payload
        batch = kw_list[i:i+5]
        pytrends.build_payload(batch, cat=0, timeframe=timeframe, geo=geo, gprop="")
        df = pytrends.interest_over_time()
        if df is None or df.empty:
            continue
        if "isPartial" in df.columns:
            df = df.drop(columns=["isPartial"])
        df = df.reset_index().melt(id_vars="date", var_name="keyword", value_name="trend")
        frames.append(df)
        time.sleep(sleep)
    if not frames:
        return pd.DataFrame(columns=["date", "keyword", "trend"])
    out = pd.concat(frames, ignore_index=True).sort_values(["keyword", "date"]).reset_index(drop=True)
    return out

# --------- Feature engineering ----------
def add_signal_features(df: pd.DataFrame, w_ma: int = 4, w_z: int = 12, yoy_lag: int = 52) -> pd.DataFrame:
    df = df.copy()
    df["trend"] = pd.to_numeric(df["trend"], errors="coerce").fillna(0)
    out = []
    for kw, g in df.groupby("keyword", group_keys=False):
        g = g.sort_values("date")
        g["trend_ma"] = g["trend"].rolling(w_ma, min_periods=max(1, w_ma//2)).mean()
        g["yoy_idx"] = (g["trend"] / g["trend"].shift(yoy_lag) * 100.0).replace([np.inf, -np.inf], np.nan)
        m = g["trend_ma"].rolling(w_z, min_periods=max(3, w_z//3)).mean()
        s = g["trend_ma"].rolling(w_z, min_periods=max(3, w_z//3)).std(ddof=0)
        g["z_score"] = (g["trend_ma"] - m) / s
        out.append(g)
    out = pd.concat(out, ignore_index=True)
    out["month"] = pd.to_datetime(out["date"]).dt.to_period("M").astype(str)
    return out

# --------- Aggregation & scoring ----------
def monthly_agg(df: pd.DataFrame, crit_z: float = 1.2) -> pd.DataFrame:
    agg = (df.groupby(["keyword","month"], as_index=False)
             .agg(avg_trend=("trend_ma","mean"),
                  avg_yoy=("yoy_idx","mean"),
                  avg_z=("z_score","mean"),
                  days=("date","count"),
                  hot_days=("z_score", lambda s: int(((s>=crit_z) & (s.notna())).sum()))))
    agg["hot_share"] = agg["hot_days"] / agg["days"]
    return agg

def minmax(x: pd.Series) -> pd.Series:
    x = x.astype(float)
    lo, hi = np.nanmin(x), np.nanmax(x)
    return (x - lo) / (hi - lo) if hi > lo else np.zeros_like(x)

def activation_score(agg: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    tmp = agg.copy()
    # Clamp to robust ranges
    tmp["z_scaled"]   = minmax(tmp["avg_z"].clip(-3, 5))
    tmp["yoy_scaled"] = minmax(tmp["avg_yoy"].clip(80, 200))
    tmp["act_score"]  = 0.6*tmp["z_scaled"] + 0.3*tmp["yoy_scaled"] + 0.1*tmp["hot_share"]
    top_months = (tmp.sort_values(["keyword","act_score"], ascending=[True, False])
                    .groupby("keyword", as_index=False)
                    .head(3)[["keyword","month","act_score","avg_yoy","avg_z","hot_share"]])
    return tmp, top_months, tmp.pivot(index="keyword", columns="month", values="act_score").fillna(0)

# --------- Exports ----------
def export_excel(signals: pd.DataFrame, agg_scored: pd.DataFrame, top_months: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    preview = signals.loc[:, ["date","keyword","trend","trend_ma","yoy_idx","z_score"]].head(1500)
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        preview.to_excel(writer, index=False, sheet_name="Signals_preview")
        agg_scored.sort_values(["keyword","month"]).to_excel(writer, index=False, sheet_name="Activation_Radar")
        top_months.to_excel(writer, index=False, sheet_name="Top_Months")
        # Autosize columns
        def autosize(ws, df):
            for j, col in enumerate(df.columns):
                header = len(str(col)) + 2
                sample = df[col].astype(str)
                p90 = int(sample.str.len().quantile(0.9)) + 2 if not sample.empty else 12
                width = max(12, min(40, header, p90))
                ws.set_column(j, j, width)
        autosize(writer.sheets["Signals_preview"], preview)
        autosize(writer.sheets["Activation_Radar"], agg_scored)
        autosize(writer.sheets["Top_Months"], top_months)

def export_plots(signals: pd.DataFrame, pivot_scores: pd.DataFrame, kws: List[str], geo: str, assets_dir: Path) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)
    # Trends preview
    plt.figure(figsize=(8,4))
    for kw in kws[:3]:
        g = signals.loc[signals["keyword"]==kw]
        plt.plot(pd.to_datetime(g["date"]), g["trend_ma"], label=kw)
    plt.title(f"Retail Trends — {geo} (smoothed)")
    plt.xlabel("Date"); plt.ylabel("Interest"); plt.legend(loc="upper left")
    plt.tight_layout()
    plt.savefig(assets_dir / "trends_preview.png", dpi=160)
    plt.close()

    # Activation radar
    plt.figure(figsize=(14, 5 + 0.4*pivot_scores.shape[0]))
    sns.heatmap(pivot_scores, cmap="viridis", linewidths=.3, cbar_kws={"label": "Activation score"})
    plt.title("Activation Radar — momentum by month")
    plt.xlabel("Month"); plt.ylabel("Keyword")
    plt.tight_layout()
    plt.savefig(assets_dir / "activation_radar.png", dpi=160)
    plt.close()

# --------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="Generate Retail Trend & Activation Radar artifacts.")
    ap.add_argument("--geo", default="US", help="Country/region code for Google Trends (e.g., US, MX).")
    ap.add_argument("--keywords", default="sneakers,laptops,furniture,cosmetics,groceries",
                    help="Comma-separated keyword list.")
    ap.add_argument("--timeframe", default="today 5-y", help='Google Trends timeframe (e.g., "today 5-y").')
    ap.add_argument("--out", default="reports/activation_radar.xlsx", help="Output Excel path.")
    args = ap.parse_args()

    kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not kws:
        print("ERROR: provide at least one keyword via --keywords", file=sys.stderr)
        sys.exit(2)

    print(f"[INFO] Fetching trends for {kws} | geo={args.geo} | timeframe={args.timeframe}")
    raw = fetch_trends(kws, geo=args.geo, timeframe=args.timeframe)
    if raw.empty:
        print("ERROR: no data returned from Google Trends. Try different keywords, geo, or timeframe.", file=sys.stderr)
        sys.exit(3)

    signals = add_signal_features(raw)
    agg = monthly_agg(signals, crit_z=1.2)
    scored, top_months, pv = activation_score(agg)

    # Persist CSV + Excel + images
    data_path = Path("data/trends_signals.csv")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    signals.to_csv(data_path, index=False)

    excel_path = Path(args.out)
    export_excel(signals, scored, top_months, excel_path)
    export_plots(signals, pv, kws, args.geo, Path("assets"))

    print(f"[OK] CSV:   {data_path.as_posix()}")
    print(f"[OK] Excel: {excel_path.as_posix()}")
    print(f"[OK] PNGs:  assets/trends_preview.png, assets/activation_radar.png")

if __name__ == "__main__":
    main()