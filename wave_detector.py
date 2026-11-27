from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@dataclass
class WaveInfo:
    stock_id: str
    ticker: str
    trough_date: str         # 波段起漲前最低點日期
    trough_price: float      # 波段最低價
    observation_start: str   # 含起漲前整理期 (往前推 pre_wave_days 交易日) 的觀察起點
    latest_date: str
    latest_close: float
    today_change_pct: float  # 個股當日漲跌幅 (%)
    today_volume_lots: int   # 個股當日成交張數
    today_avg_price: float   # 個股當日均價 (OHLC/4)
    wave_total_volume_lots: int  # 觀察區間總成交張數
    total_shares_lots: int   # 發行總張數 (若無法取得為 0)
    gain_from_trough_pct: float
    date_20d: str
    date_60d: str
    date_120d: str
    trading_dates: list[str]
    daily_avg_prices: dict[str, float]  # YYYY-MM-DD -> 當日均價 (OHLC/4)


def fetch_price_history(stock_id: str, period: str = "1y") -> tuple[str, pd.DataFrame]:
    """Fetch daily OHLCV for a Taiwan stock, trying .TW (TWSE) then .TWO (TPEx)."""
    for suffix in (".TW", ".TWO"):
        ticker = f"{stock_id}{suffix}"
        df = yf.Ticker(ticker).history(period=period)
        if df is not None and not df.empty:
            return ticker, df
    raise ValueError(f"無法從 Yahoo Finance 取得 {stock_id} (.TW/.TWO) 的日K線資料")


def detect_wave_window(
    stock_id: str,
    wave_search_days: int = 120,
    pre_wave_days: int = 30,
    explicit_start: Optional[str] = None,
) -> WaveInfo:
    """
    Find the lowest low within the past ~`wave_search_days` trading days,
    then step back `pre_wave_days` (20~40) trading days to capture pre-breakout
    consolidation accumulation. Also returns 20d/60d/120d start dates and daily
    average prices for weighted cost calculation.
    """
    ticker, df = fetch_price_history(stock_id, period="1y")
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)

    latest_dt = df.index[-1]
    latest_date = latest_dt.strftime("%Y-%m-%d")
    latest_close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else latest_close
    today_change_pct = (
        (latest_close - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
    )
    today_volume_lots = int(round(float(df["Volume"].iloc[-1]) / 1000.0))

    # Build daily average price map (OHLC/4)
    avg_series = (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4.0
    daily_avg_prices = {
        dt.strftime("%Y-%m-%d"): round(float(val), 2)
        for dt, val in avg_series.items()
    }
    today_avg_price = daily_avg_prices.get(latest_date, round(latest_close, 2))

    total_shares_lots = 0
    try:
        shares = yf.Ticker(ticker).fast_info.shares
        if shares and shares > 0:
            total_shares_lots = int(round(float(shares) / 1000.0))
    except Exception:
        total_shares_lots = 0

    def _nth_trading_day_ago(n: int) -> str:
        idx = max(0, len(df) - n)
        return df.index[idx].strftime("%Y-%m-%d")

    date_20d = _nth_trading_day_ago(20)
    date_60d = _nth_trading_day_ago(60)
    date_120d = _nth_trading_day_ago(120)

    def _calc_bottom_obs_idx(t_idx: int, t_price: float) -> int:
        # Step back up to pre_wave_days, but stop if price rises above 1.2x trough_price
        # so pre-crash high-altitude bagholders are not counted as bottom accumulation.
        limit_idx = max(0, t_idx - pre_wave_days)
        obs = t_idx
        price_cap = t_price * 1.20 if t_price > 0 else float("inf")
        for idx in range(t_idx - 1, limit_idx - 1, -1):
            if float(df["Close"].iloc[idx]) > price_cap:
                break
            obs = idx
        return obs

    if explicit_start:
        target_dt = pd.to_datetime(explicit_start)
        subset = df[df.index >= target_dt]
        if subset.empty:
            trough_idx = 0
        else:
            trough_idx = df.index.get_loc(subset.index[0])
        trough_dt = df.index[trough_idx]
        trough_price = float(df["Low"].iloc[trough_idx])
        obs_idx = max(0, trough_idx - pre_wave_days)
        obs_start = df.index[obs_idx].strftime("%Y-%m-%d")
    else:
        search_start_idx = max(0, len(df) - wave_search_days)
        # Exclude the last 1 day if there are enough rows so we anchor on a completed trough
        search_end_idx = len(df) - 1 if len(df) > 10 else len(df)
        search_slice = df.iloc[search_start_idx:search_end_idx]

        trough_dt = search_slice["Low"].idxmin()
        trough_idx = df.index.get_loc(trough_dt)
        trough_price = float(df.loc[trough_dt, "Low"])

        obs_idx = _calc_bottom_obs_idx(trough_idx, trough_price)
        obs_start = df.index[obs_idx].strftime("%Y-%m-%d")

    trading_dates = [dt.strftime("%Y-%m-%d") for dt in df.index]

    wave_slice = df.iloc[obs_idx:]
    wave_total_volume_lots = int(round(float(wave_slice["Volume"].sum()) / 1000.0))

    gain_pct = (
        (latest_close - trough_price) / trough_price * 100.0
        if trough_price > 0
        else 0.0
    )

    return WaveInfo(
        stock_id=stock_id,
        ticker=ticker,
        trough_date=trough_dt.strftime("%Y-%m-%d"),
        trough_price=round(trough_price, 2),
        observation_start=obs_start,
        latest_date=latest_date,
        latest_close=round(latest_close, 2),
        today_change_pct=round(today_change_pct, 2),
        today_volume_lots=today_volume_lots,
        today_avg_price=round(today_avg_price, 2),
        wave_total_volume_lots=wave_total_volume_lots,
        total_shares_lots=total_shares_lots,
        gain_from_trough_pct=round(gain_pct, 2),
        date_20d=date_20d,
        date_60d=date_60d,
        date_120d=date_120d,
        trading_dates=trading_dates,
        daily_avg_prices=daily_avg_prices,
    )
