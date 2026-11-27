from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from broker_scraper import (
    BigHolderWeekly,
    BranchDailyRecord,
    BrokerRangeReport,
    BrokerSummaryRow,
    ChipFlowSummary,
    fetch_branch_daily_history,
    fetch_chip_flow_summary,
    fetch_range_brokers,
    fetch_weekly_big_holders,
    is_foreign_broker,
)
from daytrade_filter import DayTradeEval, evaluate_day_trader
from wave_detector import WaveInfo, detect_wave_window


@dataclass
class TopBranchStatus:
    rank: int
    name: str
    is_foreign: bool
    is_high_turnover: bool          # 高周轉/避險 (外資分點依持有天數與淨買比分類)
    turnover_reasons: list[str]
    wave_net_lots: int              # 波段區間累計淨買超 (已扣掉賣出)
    avg_cost: float                 # FIFO 剩餘持股加權平均成本
    profit_pct: float               # 現價較成本高出 %
    profit_tier: str                # doubled (>=100%), high (>=50%), warn (>=30%), trapped (<=-10%), normal
    is_high_profit_risk: bool       # >= 30%
    today_price_vs_cost_pct: float  # 個股今日均價 vs 分點成本 %
    today_buy_lots: int
    today_sell_lots: int
    today_net_lots: int
    is_new_position: bool           # 異動前持股 <= 0 或 < 20 張
    today_net_pct_of_holding: Optional[float]  # 今日淨買賣佔「異動前持股」% (新進場為 None)
    today_net_pct_of_volume: float  # 今日淨買賣佔「當日個股成交量」%
    today_net_pct_of_capital: float # 今日淨買賣佔「發行股本」%
    avg_20d_buy_lots: float
    avg_20d_sell_lots: float
    consecutive_net_sell_days: int
    consecutive_net_sell_lots: int
    consecutive_net_buy_days: int
    consecutive_net_buy_lots: int
    last_10d_holdings: list[int]    # 最近 10 個市場交易日累計持有走勢
    last_10d_change: int            # 近 10 日持股變化張數
    period_tags: list[str]
    period_count: int
    is_geo_broker: bool
    score: float
    early_accum_ratio: Optional[float] = None
    buy_5d_win_rate: Optional[float] = None
    buy_5d_sample_count: int = 0
    is_key_broker: bool = False
    key_broker_reasons: list[str] = field(default_factory=list)
    sell_alerts: list[str] = field(default_factory=list)
    buy_alerts: list[str] = field(default_factory=list)


@dataclass
class TopSellerStatus:
    rank: int
    name: str
    is_foreign: bool
    buy_lots: int
    sell_lots: int
    net_lots: int
    volume_pct: float


@dataclass
class ExcludedDayTrader:
    name: str
    wave_net_lots: int
    eval_info: DayTradeEval


@dataclass
class StockChipMonitorResult:
    stock_id: str
    stock_name: str
    data_date: str
    wave_info: WaveInfo
    top_branches: list[TopBranchStatus]
    foreign_branches: list[TopBranchStatus]
    top_branches_total_lots: int
    concentration_wave_vol_pct: float
    concentration_capital_pct: float
    has_clear_domestic_main_force: bool
    chip_flow: Optional[ChipFlowSummary]
    weekly_big_holders: Optional[BigHolderWeekly]
    official_inst_net: int
    is_chips_to_retail: bool        # 外資賣超 + 融資增加 (僅於當日融資資料已公布時判定)
    excluded_day_traders: list[ExcludedDayTrader]
    top3_net_sell_count: int
    top3_net_buy_count: int
    has_resonance_sell: bool
    has_resonance_buy: bool
    overall_status: str             # divergent, sell, buy, stable
    should_notify_telegram: bool
    alerts: list[str] = field(default_factory=list)
    buy_alerts: list[str] = field(default_factory=list)
    foreign_alerts: list[str] = field(default_factory=list)
    foreign_buy_alerts: list[str] = field(default_factory=list)
    top_sellers: list[TopSellerStatus] = field(default_factory=list)


def _rank_map(rows: list[BrokerSummaryRow]) -> dict[str, int]:
    return {row.name: idx for idx, row in enumerate(rows, start=1)}


def _compute_fifo_remaining_cost(
    wave_records: list[BranchDailyRecord],
    daily_avg_prices: dict[str, float],
    fallback_price: float,
) -> float:
    inventory: deque[list[float]] = deque()
    total_buy_cost = 0.0
    total_buy_lots = 0

    for rec in wave_records:
        price = daily_avg_prices.get(rec.date, fallback_price)
        if rec.buy_lots > 0:
            inventory.append([float(rec.buy_lots), price])
            total_buy_cost += rec.buy_lots * price
            total_buy_lots += rec.buy_lots
        sell_rem = float(rec.sell_lots)
        while sell_rem > 0 and inventory:
            avail = inventory[0][0]
            if avail <= sell_rem:
                sell_rem -= avail
                inventory.popleft()
            else:
                inventory[0][0] -= sell_rem
                sell_rem = 0.0

    rem_lots = sum(item[0] for item in inventory)
    if rem_lots > 0:
        rem_cost = sum(item[0] * item[1] for item in inventory)
        return round(rem_cost / rem_lots, 2)
    if total_buy_lots > 0:
        return round(total_buy_cost / total_buy_lots, 2)
    return round(fallback_price, 2)


def _consecutive_streaks_aligned(
    records: list[BranchDailyRecord],
    trading_dates: list[str],
    data_date: str,
) -> tuple[int, int, int, int]:
    """
    Count consecutive net-sell / net-buy days ending on `data_date`
    strictly along market `trading_dates`. Any day with no trade (0) breaks the streak.
    """
    rec_map = {r.date: r.net_lots for r in records}
    if data_date in trading_dates:
        end_idx = trading_dates.index(data_date)
    else:
        end_idx = len(trading_dates) - 1

    sell_days = 0
    sell_lots = 0
    for idx in range(end_idx, -1, -1):
        net = rec_map.get(trading_dates[idx], 0)
        if net < 0:
            sell_days += 1
            sell_lots += abs(net)
        else:
            break

    buy_days = 0
    buy_lots = 0
    for idx in range(end_idx, -1, -1):
        net = rec_map.get(trading_dates[idx], 0)
        if net > 0:
            buy_days += 1
            buy_lots += net
        else:
            break

    return sell_days, sell_lots, buy_days, buy_lots


def _compute_last_10d_holdings(
    wave_records: list[BranchDailyRecord],
    trading_dates: list[str],
    obs_start: str,
    data_date: str,
) -> tuple[list[int], int]:
    """Map cumulative holding onto the last 10 market trading days."""
    window_dates = [d for d in trading_dates if obs_start <= d <= data_date]
    if not window_dates:
        return [], 0
    rec_map = {r.date: r.net_lots for r in wave_records}
    cum = 0
    series: list[int] = []
    for d in window_dates:
        cum += rec_map.get(d, 0)
        series.append(cum)
    last_10 = series[-10:]
    chg = (last_10[-1] - last_10[0]) if len(last_10) >= 2 else 0
    return last_10, chg


def _compute_early_accum_ratio(
    wave_records: list[BranchDailyRecord],
    trading_dates: list[str],
    obs_start: str,
    trough_date: str,
    data_date: str,
    wave_holding: int,
    min_obs_days: int = 10,
) -> Optional[float]:
    """Ratio (%) of peak/current wave holding accumulated before or near trough_date (+5 trading days).
    Returns None if the observation window has fewer than `min_obs_days` trading days.
    """
    obs_days = sum(1 for d in trading_dates if obs_start <= d <= data_date)
    if not wave_records or obs_days < min_obs_days:
        return None
    trough_idx = next((i for i, d in enumerate(trading_dates) if d >= trough_date), None)
    if trough_idx is not None:
        cutoff_idx = min(len(trading_dates) - 1, trough_idx + 5)
        cutoff_date = trading_dates[cutoff_idx]
    else:
        cutoff_date = trough_date

    cum = 0
    peak_holding = 0
    early_net = 0
    for r in wave_records:
        cum = max(0, cum + r.net_lots)
        if cum > peak_holding:
            peak_holding = cum
        if r.date <= cutoff_date:
            early_net += r.net_lots

    denom = max(peak_holding, wave_holding)
    if denom <= 0 or early_net <= 0:
        return 0.0
    return round(min(100.0, early_net / denom * 100.0), 1)


def _compute_buy_5d_win_rate(
    full_records: list[BranchDailyRecord],
    trading_dates: list[str],
    daily_avg_prices: dict[str, float],
) -> tuple[Optional[float], int, int]:
    """Compute 5-trading-day forward win rate (%) after notable net-buy days."""
    pos_lots = [r.net_lots for r in full_records if r.net_lots > 0]
    if len(pos_lots) < 3:
        return None, 0, 0

    avg_pos = sum(pos_lots) / len(pos_lots)
    min_lot = max(5, int(avg_pos * 0.5))
    candidates = [r for r in full_records if r.net_lots >= min_lot]
    if len(candidates) < 3:
        candidates = [r for r in full_records if r.net_lots > 0]

    date_to_idx = {d: i for i, d in enumerate(trading_dates)}
    wins = 0
    samples = 0
    for r in candidates:
        idx = date_to_idx.get(r.date)
        if idx is None or idx + 5 >= len(trading_dates):
            continue
        p0 = daily_avg_prices.get(r.date, 0.0)
        p5 = daily_avg_prices.get(trading_dates[idx + 5], 0.0)
        if p0 > 0 and p5 > 0:
            samples += 1
            if p5 > p0:
                wins += 1

    if samples < 3:
        return None, wins, samples
    return round(wins / samples * 100.0, 1), wins, samples


def _evaluate_key_broker(
    is_high_turnover: bool,
    wave_holding: int,
    profit_pct: float,
    period_count: int,
    is_geo: bool,
    early_accum_ratio: Optional[float],
    buy_5d_win_rate: Optional[float],
    buy_5d_wins: int,
    buy_5d_samples: int,
) -> tuple[bool, list[str]]:
    if is_high_turnover or wave_holding <= 0:
        return False, []

    # One-vote veto: if sufficient history (>= 5 samples) shows poor win rate (< 50%), never award key broker
    if buy_5d_win_rate is not None and buy_5d_samples >= 5 and buy_5d_win_rate < 50.0:
        return False, []

    reasons: list[str] = []
    strong = False

    if early_accum_ratio is not None:
        if early_accum_ratio >= 50.0 and (wave_holding >= 50 or profit_pct >= 20.0):
            reasons.append(f"領先建倉 {early_accum_ratio:.0f}%")
            strong = True
        elif early_accum_ratio >= 35.0:
            reasons.append(f"領先建倉 {early_accum_ratio:.0f}%")

    if buy_5d_win_rate is not None and buy_5d_win_rate >= 65.0 and buy_5d_samples >= 4:
        reasons.append(f"買後5日勝率 {buy_5d_win_rate:.0f}% ({buy_5d_wins}/{buy_5d_samples})")
        strong = True
    elif buy_5d_win_rate is not None and buy_5d_win_rate >= 60.0 and buy_5d_samples >= 3:
        reasons.append(f"買後5日勝率 {buy_5d_win_rate:.0f}% ({buy_5d_wins}/{buy_5d_samples})")

    if period_count >= 2 and profit_pct >= 25.0:
        reasons.append(f"多週期長抱 (獲利 {profit_pct:+.0f}%)")
        strong = True

    if is_geo:
        reasons.append("地緣波段主力")

    is_key = strong or (len(reasons) >= 2)
    return is_key, reasons if is_key else []


def _classify_profit_tier(profit_pct: float) -> str:
    if profit_pct >= 100.0:
        return "doubled"
    if profit_pct >= 50.0:
        return "high"
    if profit_pct >= 30.0:
        return "warn"
    if profit_pct <= -10.0:
        return "trapped"
    return "normal"


def _build_branch_status(
    br: BrokerSummaryRow,
    wave: WaveInfo,
    data_date: str,
    full_records: list[BranchDailyRecord],
    wave_records: list[BranchDailyRecord],
    rank_20: dict[str, int],
    rank_60: dict[str, int],
    rank_120: dict[str, int],
    geo_kws: list[str],
    geo_boost: float,
    profit_risk_pct: float,
    is_foreign: bool,
    is_high_turnover: bool = False,
    turnover_reasons: Optional[list[str]] = None,
) -> tuple[float, TopBranchStatus]:
    tags: list[str] = []
    p_count = 0
    if br.name in rank_20:
        tags.append(f"20日#{rank_20[br.name]}")
        p_count += 1
    if br.name in rank_60:
        tags.append(f"60日#{rank_60[br.name]}")
        p_count += 1
    if br.name in rank_120:
        tags.append(f"120日#{rank_120[br.name]}")
        p_count += 1

    is_geo = any(kw in br.name for kw in geo_kws)
    period_mult = 1.0 + 0.25 * max(0, p_count - 1)
    geo_mult = geo_boost if is_geo else 1.0

    if wave_records:
        hold = 0
        for rec in wave_records:
            hold = max(0, hold + rec.net_lots)
        wave_holding = hold
    else:
        wave_holding = max(0, br.net_lots)
    score = float(wave_holding) * period_mult * geo_mult

    avg_cost = _compute_fifo_remaining_cost(
        wave_records, wave.daily_avg_prices, wave.latest_close
    )
    profit_pct = (
        (wave.latest_close - avg_cost) / avg_cost * 100.0 if avg_cost > 0 else 0.0
    )
    profit_tier = _classify_profit_tier(profit_pct)
    today_vs_cost_pct = (
        (wave.today_avg_price - avg_cost) / avg_cost * 100.0 if avg_cost > 0 else 0.0
    )

    today_rec = next((r for r in full_records if r.date == data_date), None)
    today_buy = today_rec.buy_lots if today_rec else 0
    today_sell = today_rec.sell_lots if today_rec else 0
    today_net = today_rec.net_lots if today_rec else 0

    pre_trade_holding = max(0, wave_holding - today_net)
    is_new_position = pre_trade_holding < 20
    today_net_pct: Optional[float] = (
        None if is_new_position else round(today_net / pre_trade_holding * 100.0, 2)
    )
    net_pct_vol = (
        today_net / wave.today_volume_lots * 100.0 if wave.today_volume_lots > 0 else 0.0
    )
    net_pct_cap = (
        today_net / wave.total_shares_lots * 100.0 if wave.total_shares_lots > 0 else 0.0
    )

    recent_20 = full_records[-20:] if full_records else []
    avg_20d_buy = (
        sum(r.buy_lots for r in recent_20) / len(recent_20) if recent_20 else 0.0
    )
    avg_20d_sell = (
        sum(r.sell_lots for r in recent_20) / len(recent_20) if recent_20 else 0.0
    )

    consec_sell, consec_sell_lots, consec_buy, consec_buy_lots = (
        _consecutive_streaks_aligned(full_records, wave.trading_dates, data_date)
    )

    # Market-impact threshold: >= 30 lots AND (>= 2% of today's volume OR >= 0.1% of capital)
    vol_floor_2pct = max(30, int(wave.today_volume_lots * 0.02))
    cap_floor_01pct = (
        max(30, int(wave.total_shares_lots * 0.001))
        if wave.total_shares_lots > 0
        else vol_floor_2pct
    )
    market_floor = min(vol_floor_2pct, cap_floor_01pct)

    branch_alerts: list[str] = []
    branch_buy_alerts: list[str] = []

    # High-turnover / hedging foreign branches are normally muted, EXCEPT when they do extreme one-way dumping
    net_sell_lots = abs(today_net) if today_net < 0 else 0
    ht_sell_override = (
        is_foreign
        and is_high_turnover
        and (
            consec_sell >= 5
            or (
                net_sell_lots >= max(30, int(wave.today_volume_lots * 0.03))
                and pre_trade_holding > 0
                and net_sell_lots >= pre_trade_holding * 0.30
            )
        )
    )
    if not (is_foreign and is_high_turnover) or ht_sell_override:
        if net_sell_lots >= market_floor:
            prefix = "[高周轉異常倒貨] " if ht_sell_override else ""
            if pre_trade_holding >= 100 and net_sell_lots >= pre_trade_holding * 0.10:
                net_sell_pct = net_sell_lots / pre_trade_holding * 100.0
                branch_alerts.append(
                    f"{prefix}單日淨賣超 {net_sell_lots:,} 張 (佔當日量 {abs(net_pct_vol):.1f}%、賣前持股 {net_sell_pct:.1f}%)"
                )
            else:
                branch_alerts.append(
                    f"{prefix}單日大額淨賣 {net_sell_lots:,} 張 (佔當日量 {abs(net_pct_vol):.1f}%)"
                )
        if (
            avg_20d_sell > 0
            and net_sell_lots >= market_floor
            and today_sell >= avg_20d_sell * 2.0
            and net_sell_lots >= today_sell * 0.5
        ):
            branch_alerts.append(
                f"單日賣出 {today_sell:,} 張 ≥ 20日均賣2倍 ({avg_20d_sell:.0f}×2，淨賣{net_sell_lots:,})"
            )
        pre_consec_sell_holding = max(wave_holding + consec_sell_lots, 1)
        if (
            consec_sell >= 2
            and consec_sell_lots >= market_floor
            and consec_sell_lots >= pre_consec_sell_holding * 0.05
        ):
            branch_alerts.append(f"連續 {consec_sell} 天淨賣(淨賣超{consec_sell_lots:,}張)")

        net_buy_lots = today_net if today_net > 0 else 0
        if net_buy_lots >= market_floor:
            if is_new_position:
                branch_buy_alerts.append(
                    f"🆕 新進場建倉 (淨買超 {net_buy_lots:,} 張，佔當日量 {net_pct_vol:.1f}%)"
                )
            elif pre_trade_holding >= 100 and net_buy_lots >= pre_trade_holding * 0.10:
                net_buy_pct = net_buy_lots / pre_trade_holding * 100.0
                branch_buy_alerts.append(
                    f"單日淨買超 {net_buy_lots:,} 張 (佔當日量 {net_pct_vol:.1f}%、買前持股 {net_buy_pct:.1f}%)"
                )
            else:
                branch_buy_alerts.append(
                    f"單日大額淨買 {net_buy_lots:,} 張 (佔當日量 {net_pct_vol:.1f}%)"
                )
            if (
                avg_20d_buy > 0
                and today_buy >= avg_20d_buy * 2.0
                and net_buy_lots >= today_buy * 0.5
            ):
                branch_buy_alerts.append(
                    f"單日買進 {today_buy:,} 張 ≥ 20日均買2倍 ({avg_20d_buy:.0f}×2，淨買{net_buy_lots:,})"
                )

        if consec_buy >= 3 and consec_buy_lots >= market_floor:
            branch_buy_alerts.append(f"連續 {consec_buy} 天淨買(淨買超{consec_buy_lots:,}張)")
        elif (
            consec_buy >= 2
            and consec_buy_lots >= market_floor
            and (is_new_position or consec_buy_lots >= pre_trade_holding * 0.05)
        ):
            branch_buy_alerts.append(f"連續 {consec_buy} 天淨買(淨買超{consec_buy_lots:,}張)")

    last_10, chg_10 = _compute_last_10d_holdings(
        wave_records, wave.trading_dates, wave.observation_start, data_date
    )
    early_accum_ratio = _compute_early_accum_ratio(
        wave_records, wave.trading_dates, wave.observation_start, wave.trough_date, data_date, wave_holding
    )
    buy_5d_win_rate, buy_5d_wins, buy_5d_samples = _compute_buy_5d_win_rate(
        full_records, wave.trading_dates, wave.daily_avg_prices
    )
    is_key_broker, key_broker_reasons = _evaluate_key_broker(
        is_high_turnover=is_high_turnover,
        wave_holding=wave_holding,
        profit_pct=profit_pct,
        period_count=p_count,
        is_geo=is_geo,
        early_accum_ratio=early_accum_ratio,
        buy_5d_win_rate=buy_5d_win_rate,
        buy_5d_wins=buy_5d_wins,
        buy_5d_samples=buy_5d_samples,
    )

    status = TopBranchStatus(
        rank=0,
        name=br.name,
        is_foreign=is_foreign,
        is_high_turnover=is_high_turnover,
        turnover_reasons=turnover_reasons or [],
        wave_net_lots=wave_holding,
        avg_cost=avg_cost,
        profit_pct=round(profit_pct, 2),
        profit_tier=profit_tier,
        is_high_profit_risk=profit_pct >= profit_risk_pct,
        today_price_vs_cost_pct=round(today_vs_cost_pct, 2),
        today_buy_lots=today_buy,
        today_sell_lots=today_sell,
        today_net_lots=today_net,
        is_new_position=is_new_position,
        today_net_pct_of_holding=today_net_pct,
        today_net_pct_of_volume=round(net_pct_vol, 2),
        today_net_pct_of_capital=round(net_pct_cap, 3),
        avg_20d_buy_lots=round(avg_20d_buy, 1),
        avg_20d_sell_lots=round(avg_20d_sell, 1),
        consecutive_net_sell_days=consec_sell,
        consecutive_net_sell_lots=consec_sell_lots,
        consecutive_net_buy_days=consec_buy,
        consecutive_net_buy_lots=consec_buy_lots,
        last_10d_holdings=last_10,
        last_10d_change=chg_10,
        period_tags=tags,
        period_count=p_count,
        is_geo_broker=is_geo,
        score=round(score, 1),
        early_accum_ratio=early_accum_ratio,
        buy_5d_win_rate=buy_5d_win_rate,
        buy_5d_sample_count=buy_5d_samples,
        is_key_broker=is_key_broker,
        key_broker_reasons=key_broker_reasons,
        sell_alerts=branch_alerts,
        buy_alerts=branch_buy_alerts,
    )
    return score, status


def analyze_stock(
    stock_id: str,
    name_override: Optional[str] = None,
    explicit_wave_start: Optional[str] = None,
    wave_search_days: int = 120,
    pre_wave_days: int = 30,
    top_n: int = 5,
    geo_keywords: Optional[list[str]] = None,
    geo_boost: float = 1.35,
    profit_risk_pct: float = 30.0,
) -> StockChipMonitorResult:
    wave = detect_wave_window(
        stock_id=stock_id,
        wave_search_days=wave_search_days,
        pre_wave_days=pre_wave_days,
        explicit_start=explicit_wave_start,
    )

    wave_report: BrokerRangeReport = fetch_range_brokers(
        stock_id=stock_id,
        start_date=wave.observation_start,
        end_date=wave.latest_date,
    )
    data_date = wave_report.last_update_date
    stock_name = name_override or wave_report.stock_name

    r20 = fetch_range_brokers(stock_id, wave.date_20d, data_date)
    r60 = fetch_range_brokers(stock_id, wave.date_60d, data_date)
    r120 = fetch_range_brokers(stock_id, wave.date_120d, data_date)
    rank_20 = _rank_map(r20.buy_top)
    rank_60 = _rank_map(r60.buy_top)
    rank_120 = _rank_map(r120.buy_top)

    chip_flow = fetch_chip_flow_summary(stock_id)
    weekly_big_holders = fetch_weekly_big_holders(stock_id)
    official_inst_net = (
        (chip_flow.foreign_chg + chip_flow.trust_chg + chip_flow.dealer_chg)
        if chip_flow
        else 0
    )
    is_chips_to_retail = bool(
        chip_flow
        and chip_flow.margin_available
        and chip_flow.foreign_chg < 0
        and chip_flow.margin_chg > 0
    )

    history_start = min(wave.observation_start, wave.date_60d)
    geo_kws = geo_keywords or []

    domestic_list: list[TopBranchStatus] = []
    foreign_list: list[TopBranchStatus] = []
    excluded_day_traders: list[ExcludedDayTrader] = []

    for br in wave_report.buy_top:
        if br.net_lots <= 0:
            continue

        full_records = (
            fetch_branch_daily_history(br.zco0_href, history_start, data_date)
            if br.zco0_href
            else []
        )
        wave_records = [r for r in full_records if r.date >= wave.observation_start]
        wave_holding = sum(r.net_lots for r in wave_records) if wave_records else br.net_lots
        if wave_holding <= 0:
            continue

        foreign = is_foreign_broker(br.name)
        dt_eval = evaluate_day_trader(full_records[-60:] if full_records else [])

        if not foreign and dt_eval.is_day_trader:
            excluded_day_traders.append(
                ExcludedDayTrader(
                    name=br.name,
                    wave_net_lots=br.net_lots,
                    eval_info=dt_eval,
                )
            )
            continue

        _, status = _build_branch_status(
            br=br,
            wave=wave,
            data_date=data_date,
            full_records=full_records,
            wave_records=wave_records,
            rank_20=rank_20,
            rank_60=rank_60,
            rank_120=rank_120,
            geo_kws=geo_kws,
            geo_boost=geo_boost,
            profit_risk_pct=profit_risk_pct,
            is_foreign=foreign,
            is_high_turnover=(foreign and dt_eval.is_day_trader),
            turnover_reasons=dt_eval.reasons if (foreign and dt_eval.is_day_trader) else [],
        )

        if foreign:
            foreign_list.append(status)
        else:
            domestic_list.append(status)

    # Sort strictly by 目前持有張數 (wave_net_lots) descending
    domestic_list.sort(key=lambda b: b.wave_net_lots, reverse=True)
    foreign_list.sort(key=lambda b: b.wave_net_lots, reverse=True)

    top_branches: list[TopBranchStatus] = []
    alerts: list[str] = []
    buy_alerts: list[str] = []

    for idx, st in enumerate(domestic_list[:top_n], start=1):
        st.rank = idx
        top_branches.append(st)
        for al in st.sell_alerts:
            alerts.append(f"第{idx}主力 [{st.name}] {al}")
        for bal in st.buy_alerts:
            buy_alerts.append(f"第{idx}主力 [{st.name}] {bal}")

    foreign_branches: list[TopBranchStatus] = []
    foreign_alerts: list[str] = []
    foreign_buy_alerts: list[str] = []
    for idx, fst in enumerate(foreign_list, start=1):
        fst.rank = idx
        foreign_branches.append(fst)
        for fal in fst.sell_alerts:
            foreign_alerts.append(f"外資 [{fst.name}] {fal}")
        for fbal in fst.buy_alerts:
            foreign_buy_alerts.append(f"外資 [{fst.name}] {fbal}")

    # Significant net sell for resonance: >= 30 lots AND >= 5% of pre-trade holding
    sig_sellers = [
        b
        for b in top_branches
        if b.today_net_lots < 0
        and abs(b.today_net_lots) >= 30
        and abs(b.today_net_lots) >= max(b.wave_net_lots - b.today_net_lots, 1) * 0.05
    ]
    top3_net_sell_count = len(sig_sellers)
    has_resonance_sell = top3_net_sell_count >= 2
    if has_resonance_sell:
        sellers = "、".join(b.name for b in sig_sellers)
        alerts.append(
            f"前{top_n}大中有 {top3_net_sell_count} 家同日顯著淨賣（共振出貨：{sellers}）"
        )

    # Significant net buy for resonance: >= 30 lots AND >= 5% of pre-trade holding (or new position >= 30)
    sig_buyers = [
        b
        for b in top_branches
        if b.today_net_lots > 0
        and b.today_net_lots >= 30
        and (
            b.is_new_position
            or b.today_net_lots >= max(b.wave_net_lots - b.today_net_lots, 1) * 0.05
        )
    ]
    top3_net_buy_count = len(sig_buyers)
    has_resonance_buy = top3_net_buy_count >= 3
    if has_resonance_buy:
        buyers = "、".join(b.name for b in sig_buyers)
        buy_alerts.append(
            f"前{top_n}大中有 {top3_net_buy_count} 家同日顯著淨買（共振加碼：{buyers}）"
        )

    if is_chips_to_retail and chip_flow:
        foreign_alerts.append(
            f"⚠️ 籌碼轉向散戶：外資當日賣超 {abs(chip_flow.foreign_chg):,} 張、融資增加 {chip_flow.margin_chg:+,} 張"
        )

    official_sell_floor = max(50, int(wave.today_volume_lots * 0.05))
    official_heavy_sell = official_inst_net <= -official_sell_floor
    if official_heavy_sell and chip_flow:
        inst_vol_pct = (
            abs(official_inst_net) / wave.today_volume_lots * 100.0
            if wave.today_volume_lots > 0
            else 0.0
        )
        foreign_alerts.append(
            f"官方三大法人合計賣超 {abs(official_inst_net):,} 張 (佔當日量 {inst_vol_pct:.1f}%：外資 {chip_flow.foreign_chg:+,} / 投信 {chip_flow.trust_chg:+,})"
        )

    has_clear_domestic = (
        any(b.wave_net_lots >= wave.total_shares_lots * 0.003 for b in top_branches)
        if wave.total_shares_lots > 0
        else True
    )

    has_sell_sig = len(alerts) > 0 or len(foreign_alerts) > 0
    # Only count domestic buy signals toward overall "buy" badge if there is a clear domestic main force
    # or if foreign long-hold branches are also buying
    foreign_strong_buy = (
        chip_flow is not None
        and wave.today_volume_lots > 0
        and chip_flow.foreign_chg >= max(50, int(wave.today_volume_lots * 0.05))
    )
    if foreign_strong_buy and chip_flow is not None and not foreign_buy_alerts:
        f_vol_pct = chip_flow.foreign_chg / wave.today_volume_lots * 100.0
        foreign_buy_alerts.append(
            f"官方外資當日大買 {chip_flow.foreign_chg:+,} 張 (佔當日量 {f_vol_pct:.1f}%)"
        )

    effective_buy_sig = (
        (len(buy_alerts) > 0 and (has_clear_domestic or has_resonance_buy))
        or len(foreign_buy_alerts) > 0
        or foreign_strong_buy
    )

    top_sellers: list[TopSellerStatus] = []
    try:
        today_rep = fetch_range_brokers(stock_id, data_date, data_date)
        for idx, srow in enumerate(today_rep.sell_top[:5], start=1):
            top_sellers.append(
                TopSellerStatus(
                    rank=idx,
                    name=srow.name,
                    is_foreign=is_foreign_broker(srow.name),
                    buy_lots=srow.buy_lots,
                    sell_lots=srow.sell_lots,
                    net_lots=srow.net_lots,
                    volume_pct=srow.volume_pct,
                )
            )
    except Exception:
        top_sellers = []
    if has_sell_sig and effective_buy_sig:
        overall_status = "divergent"
    elif has_sell_sig:
        overall_status = "sell"
    elif effective_buy_sig:
        overall_status = "buy"
    else:
        overall_status = "stable"

    top_total_lots = sum(b.wave_net_lots for b in top_branches)
    conc_wave = (
        top_total_lots / wave.wave_total_volume_lots * 100.0
        if wave.wave_total_volume_lots > 0
        else 0.0
    )
    conc_cap = (
        top_total_lots / wave.total_shares_lots * 100.0
        if wave.total_shares_lots > 0
        else 0.0
    )

    return StockChipMonitorResult(
        stock_id=stock_id,
        stock_name=stock_name,
        data_date=data_date,
        wave_info=wave,
        top_branches=top_branches,
        foreign_branches=foreign_branches,
        top_branches_total_lots=top_total_lots,
        concentration_wave_vol_pct=round(conc_wave, 2),
        concentration_capital_pct=round(conc_cap, 2),
        has_clear_domestic_main_force=has_clear_domestic,
        chip_flow=chip_flow,
        weekly_big_holders=weekly_big_holders,
        official_inst_net=official_inst_net,
        is_chips_to_retail=is_chips_to_retail,
        excluded_day_traders=excluded_day_traders,
        top3_net_sell_count=top3_net_sell_count,
        top3_net_buy_count=top3_net_buy_count,
        has_resonance_sell=has_resonance_sell,
        has_resonance_buy=has_resonance_buy,
        overall_status=overall_status,
        should_notify_telegram=has_sell_sig or effective_buy_sig,
        alerts=alerts,
        buy_alerts=buy_alerts,
        foreign_alerts=foreign_alerts,
        foreign_buy_alerts=foreign_buy_alerts,
        top_sellers=top_sellers,
    )
