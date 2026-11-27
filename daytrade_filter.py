from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from broker_scraper import BranchDailyRecord


@dataclass
class DayTradeEval:
    is_day_trader: bool
    matched_rules: int
    net_buy_ratio: float
    next_day_reverse_rate: float
    avg_holding_days: float
    reasons: list[str]


def evaluate_day_trader(records: list[BranchDailyRecord]) -> DayTradeEval:
    """
    Evaluate whether a broker branch behaves like a day-trader (隔日沖)
    on THIS specific stock, based on 3 behavioral metrics:
      1. Net buy ratio: |net| / (buy + sell) < 0.20
      2. Next-day reversal rate: fraction of net-buy days followed by
         next-day sell >= 70% of previous net-buy > 60%
      3. Average holding days (FIFO): <= 2.0 trading days

    Marked as day-trader ONLY when >= 2 of the 3 metrics match.
    """
    if not records:
        return DayTradeEval(
            is_day_trader=False,
            matched_rules=0,
            net_buy_ratio=1.0,
            next_day_reverse_rate=0.0,
            avg_holding_days=99.0,
            reasons=[],
        )

    total_buy = sum(r.buy_lots for r in records)
    total_sell = sum(r.sell_lots for r in records)
    total_net = sum(r.net_lots for r in records)
    gross = total_buy + total_sell
    net_buy_ratio = abs(total_net) / gross if gross > 0 else 0.0

    # Metric 2: Next-day reversal rate
    net_buy_days = 0
    reversed_days = 0
    for i in range(len(records) - 1):
        curr = records[i]
        nxt = records[i + 1]
        if curr.net_lots > 0:
            net_buy_days += 1
            if nxt.sell_lots >= curr.net_lots * 0.70:
                reversed_days += 1

    reverse_rate = (reversed_days / net_buy_days) if net_buy_days >= 3 else 0.0

    # Metric 3: FIFO average holding days (for sold lots)
    inventory: deque[list[int]] = deque()  # [day_index, remaining_lots]
    weighted_hold_days = 0.0
    matched_sold_lots = 0

    for idx, rec in enumerate(records):
        if rec.buy_lots > 0:
            inventory.append([idx, rec.buy_lots])
        sell_rem = rec.sell_lots
        while sell_rem > 0 and inventory:
            buy_idx, avail = inventory[0]
            take = min(sell_rem, avail)
            hold_days = max(0, idx - buy_idx)
            weighted_hold_days += take * hold_days
            matched_sold_lots += take
            sell_rem -= take
            if avail == take:
                inventory.popleft()
            else:
                inventory[0][1] -= take

    turnover_ratio = (matched_sold_lots / total_buy) if total_buy > 0 else 0.0
    if matched_sold_lots > 0 and turnover_ratio >= 0.30:
        avg_holding_days = weighted_hold_days / matched_sold_lots
    else:
        # Very little selling relative to buying -> long-hold behavior
        avg_holding_days = float(len(records))

    reasons: list[str] = []
    if net_buy_ratio < 0.20:
        reasons.append(f"淨買比 {net_buy_ratio:.2f} (<0.20)")
    if net_buy_days >= 3 and reverse_rate > 0.60:
        reasons.append(f"隔日反向率 {reverse_rate * 100:.0f}% (>60%)")
    if turnover_ratio >= 0.30 and avg_holding_days <= 2.0:
        reasons.append(f"平均持有 {avg_holding_days:.1f} 天 (<=2天)")

    matched = len(reasons)
    return DayTradeEval(
        is_day_trader=matched >= 2,
        matched_rules=matched,
        net_buy_ratio=round(net_buy_ratio, 3),
        next_day_reverse_rate=round(reverse_rate * 100.0, 1),
        avg_holding_days=round(avg_holding_days, 1),
        reasons=reasons,
    )
