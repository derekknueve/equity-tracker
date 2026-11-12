#!/usr/bin/env python3
"""Fetch daily prices for the tracked securities and write data.json.

Run by the GitHub Actions workflow each weekday morning. Exits non-zero if the
data looks wrong (missing ticker, stale data, not enough history) so the
workflow fails and GitHub emails you instead of publishing bad numbers.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

# Display symbol -> (Yahoo symbol, name). Edit this list to add or remove securities.
SECURITIES = {
    "MGK": ("MGK", "Vanguard Mega Cap Growth ETF"),
    "VGT": ("VGT", "Vanguard Information Technology ETF"),
    "VTI": ("VTI", "Vanguard Total Stock Market ETF"),
    "BRK.B": ("BRK-B", "Berkshire Hathaway Class B"),
    "VOOG": ("VOOG", "Vanguard S&P 500 Growth ETF"),
}

OUT = Path(__file__).parent / "data.json"
ET = ZoneInfo("America/New_York")
MAX_STALE_DAYS = 5      # longest normal gap: a Friday holiday plus the weekend
TOLERANCE_DAYS = 7      # how far a lookback date may land from a real trading day


def fetch(yahoo_symbol: str, start: str, now: datetime) -> pd.DataFrame:
    """Daily Close (price) and Adj Close (dividend-adjusted, used for returns)."""
    import yfinance as yf

    df = yf.Ticker(yahoo_symbol).history(start=start, auto_adjust=False, actions=False)
    if df is None or df.empty or "Adj Close" not in df.columns:
        raise RuntimeError(f"No price data returned for {yahoo_symbol}")
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[["Close", "Adj Close"]].dropna()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # Before the close, drop any partial row for today so every series ends on a final close.
    if now.hour * 60 + now.minute < 16 * 60 + 30:
        df = df[df.index < pd.Timestamp(now.date())]
    if df.empty:
        raise RuntimeError(f"No completed daily bars for {yahoo_symbol}")
    return df


def at_or_before(s: pd.Series, target: pd.Timestamp):
    """Last value on or before target, or None if history doesn't reach that far."""
    prior = s[s.index <= target]
    if prior.empty or (target - prior.index[-1]).days > TOLERANCE_DAYS:
        return None
    return float(prior.iloc[-1])


def ratio(end, base):
    return None if end is None or base is None else end / base - 1


def compute_returns(adj: pd.Series, as_of: pd.Timestamp, cal_year: int) -> dict:
    end = float(adj.iloc[-1])
    month_start = pd.Timestamp(as_of.year, as_of.month, 1)
    r3 = ratio(end, at_or_before(adj, as_of - pd.DateOffset(years=3)))
    r5 = ratio(end, at_or_before(adj, as_of - pd.DateOffset(years=5)))
    return {
        "mtd": ratio(end, at_or_before(adj, month_start - pd.Timedelta(days=1))),
        "m2": ratio(end, at_or_before(adj, as_of - pd.DateOffset(months=2))),
        "cy": ratio(
            at_or_before(adj, pd.Timestamp(cal_year, 12, 31)),
            at_or_before(adj, pd.Timestamp(cal_year - 1, 12, 31)),
        ),
        "y3": r3,
        "y5": r5,
        "y3ann": None if r3 is None else (1 + r3) ** (1 / 3) - 1,
        "y5ann": None if r5 is None else (1 + r5) ** (1 / 5) - 1,
    }


def clean(values):
    return [None if pd.isna(v) else round(float(v), 4) for v in values]


def build(frames: dict, now: datetime) -> dict:
    last_dates = {sym: f.index[-1] for sym, f in frames.items()}
    if len(set(last_dates.values())) != 1:
        raise RuntimeError(f"Securities end on different dates: {last_dates}")
    as_of = next(iter(last_dates.values()))
    age = (pd.Timestamp(now.date()) - as_of).days
    if age > MAX_STALE_DAYS:
        raise RuntimeError(f"Latest close is {as_of.date()}, {age} days old")

    cal_year = as_of.year - 1
    tickers = []
    for sym, (_, name) in SECURITIES.items():
        f = frames[sym]
        returns = compute_returns(f["Adj Close"], as_of, cal_year)
        missing = [k for k, v in returns.items() if v is None]
        if missing:
            raise RuntimeError(f"{sym}: could not compute {missing} (not enough history?)")
        tickers.append({
            "symbol": sym,
            "name": name,
            "lastClose": round(float(f["Close"].iloc[-1]), 2),
            "returns": {k: round(v, 6) for k, v in returns.items()},
        })

    # Running 5-year window, starting on the trading day used as the 5-year base.
    all_dates = sorted(set().union(*[f.index for f in frames.values()]))
    cutoff = as_of - pd.DateOffset(years=5)
    start = max([d for d in all_dates if d <= cutoff], default=all_dates[0])
    dates = [d for d in all_dates if d >= start]

    return {
        "asOf": as_of.date().isoformat(),
        "generatedAt": datetime.now(ET).isoformat(timespec="seconds"),
        "calendarYear": cal_year,
        "tickers": tickers,
        "dates": [d.date().isoformat() for d in dates],
        "close": {s: clean(frames[s]["Close"].reindex(dates)) for s in SECURITIES},
        "adj": {s: clean(frames[s]["Adj Close"].reindex(dates)) for s in SECURITIES},
    }


def main() -> int:
    now = datetime.now(ET)
    start = (pd.Timestamp(now.date()) - pd.DateOffset(years=5) - pd.Timedelta(days=21)).date().isoformat()
    try:
        frames = {sym: fetch(ysym, start, now) for sym, (ysym, _) in SECURITIES.items()}
        data = build(frames, now)
    except Exception as exc:  # fail loudly so the workflow turns red and emails you
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"Wrote {OUT.name}: as of {data['asOf']}, {len(data['dates'])} trading days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
