from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from monitor import StockChipMonitorResult, TopBranchStatus


def _esc(val: object) -> str:
    return html.escape(str(val), quote=True)


def _signed_cls(val: float) -> str:
    if val > 0:
        return "num-pos"
    if val < 0:
        return "num-neg"
    return "num-zero"


def _derive_action_stance(r: StockChipMonitorResult) -> tuple[str, str, str]:
    """Return (stance_label, css_class, concise_reason) for quick intuition."""
    w = r.wave_info
    cf = r.chip_flow
    vol = w.today_volume_lots
    foreign_net = cf.foreign_chg if cf is not None else sum(b.today_net_lots for b in r.foreign_branches)
    trust_net = cf.trust_chg if cf is not None else 0
    inst_net = r.official_inst_net if cf is not None else foreign_net
    dom_net = sum(b.today_net_lots for b in r.top_branches)
    margin_chg = cf.margin_chg if (cf is not None and cf.margin_available) else None

    f_heavy_sell = foreign_net <= -max(50, int(vol * 0.05)) if vol > 0 else foreign_net < 0
    f_clear_buy = foreign_net >= max(30, int(vol * 0.02)) if vol > 0 else foreign_net > 0

    if (r.is_chips_to_retail and f_heavy_sell and dom_net <= 0) or (
        r.overall_status == "sell" and not r.has_resonance_buy and foreign_net <= 0
    ):
        parts: list[str] = []
        if foreign_net < 0:
            parts.append(f"外資 {foreign_net:+,}")
        if trust_net < 0:
            parts.append(f"投信 {trust_net:+,}")
        if dom_net < 0:
            parts.append(f"本土 {dom_net:+,}")
        if r.is_chips_to_retail and margin_chg is not None:
            parts.append(f"融資 {margin_chg:+,} 轉散戶")
        return "看空", "stance-bear", "、".join(parts) or "賣壓主導"

    bh = getattr(r, "weekly_big_holders", None)
    bh_drop = bh is not None and bh.big_holder_diff < -2.0

    if (
        (r.overall_status == "buy" or r.has_resonance_buy or f_clear_buy)
        and inst_net >= 0
        and trust_net >= 0
        and not r.is_chips_to_retail
        and not r.has_resonance_sell
        and not r.alerts
        and not bh_drop
    ):
        parts = []
        if foreign_net > 0:
            parts.append(f"外資 {foreign_net:+,}")
        if dom_net > 0:
            parts.append(f"本土 {dom_net:+,}")
        if margin_chg is not None and margin_chg < 0:
            parts.append(f"融資 {margin_chg:+,}")
        return "加碼", "stance-bull", "、".join(parts) or "買盤主導"

    buy_parts: list[str] = []
    sell_parts: list[str] = []
    if foreign_net > 0:
        buy_parts.append(f"外資 {foreign_net:+,}")
    elif foreign_net < 0:
        sell_parts.append(f"外資 {foreign_net:+,}")
    if dom_net > 0:
        buy_parts.append(f"本土 {dom_net:+,}")
    elif dom_net < 0:
        sell_parts.append(f"本土 {dom_net:+,}")
    if trust_net < -50:
        sell_parts.append(f"投信 {trust_net:+,}")
    elif trust_net > 50:
        buy_parts.append(f"投信 {trust_net:+,}")

    if bh_drop and bh is not None:
        sell_parts.append(f"集保大戶 {bh.big_holder_diff:+.2f}%")

    if buy_parts and sell_parts:
        reason = f"{'／'.join(buy_parts)} 接盤，{'／'.join(sell_parts)} 賣壓"
    elif margin_chg is not None and margin_chg > 0 and buy_parts:
        reason = f"{'／'.join(buy_parts)} 承接，融資 {margin_chg:+,}"
    elif buy_parts:
        reason = "、".join(buy_parts)
    elif sell_parts:
        reason = "、".join(sell_parts)
    else:
        reason = "籌碼變化平穩"
    return "持平", "stance-neutral", reason


def _render_period_badge(tags: list[str], count: int) -> str:
    tags_html = " ".join(f'<span class="tag-mini">{_esc(t)}</span>' for t in tags)
    if count >= 3:
        return f'<span class="badge badge-star">★ 三週期共振</span><div class="tag-row">{tags_html}</div>'
    if count == 2:
        return f'<span class="badge badge-mid">雙週期</span><div class="tag-row">{tags_html}</div>'
    if count == 1 and any(t.startswith("20日") for t in tags):
        return f'<span class="badge badge-short">僅20日短線</span><div class="tag-row">{tags_html}</div>'
    if tags:
        return f'<div class="tag-row">{tags_html}</div>'
    return '<span class="badge badge-short">波段區間</span>'


def _render_profit_tier_tag(tier: str) -> str:
    if tier == "doubled":
        return '<div class="tier-tag tier-doubled">🔥 獲利&gt;100% 翻倍</div>'
    if tier == "high":
        return '<div class="tier-tag tier-high">🔴 獲利&gt;50% 警戒</div>'
    if tier == "warn":
        return '<div class="tier-tag tier-warn">⚠️ 獲利&gt;30% 注意</div>'
    if tier == "trapped":
        return '<div class="tier-tag tier-trapped">🥶 套牢 &lt;-10%</div>'
    return ""


def _render_sparkline(values: list[int], change_10d: int) -> str:
    if not values or len(values) < 2:
        return '<span class="num-zero">-</span>'
    w, h = 72, 22
    vmin, vmax = min(values), max(values)
    span = max(vmax - vmin, 1)
    pts: list[str] = []
    for i, v in enumerate(values):
        x = 2 + (w - 4) * i / (len(values) - 1)
        y = (h - 3) - (h - 6) * (v - vmin) / span
        pts.append(f"{x:.1f},{y:.1f}")
    color = "#dc2626" if change_10d > 0 else ("#16a34a" if change_10d < 0 else "#64748b")
    last_x, last_y = pts[-1].split(",")
    chg_cls = _signed_cls(change_10d)
    chg_txt = f"{change_10d:+,}張" if change_10d != 0 else "0張"
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" title="近10個交易日累計持有走勢">'
        f'<polyline fill="none" stroke="{color}" stroke-width="1.8" points="{" ".join(pts)}" />'
        f'<circle cx="{last_x}" cy="{last_y}" r="2.2" fill="{color}" />'
        f"</svg>"
        f'<div class="sub-pct {chg_cls}">10日 {chg_txt}</div>'
    )


def _render_branch_rows(branches: list[TopBranchStatus], today_avg_price: float) -> str:
    rows_html: list[str] = []
    max_holding = max((b.wave_net_lots for b in branches), default=1)
    for br in branches:
        geo_badge = '<span class="badge badge-geo">地緣</span>' if br.is_geo_broker else ""
        key_reasons = getattr(br, "key_broker_reasons", []) or []
        key_title = f' title="{_esc("、".join(key_reasons))}"' if key_reasons else ""
        key_badge = (
            f'<span class="badge badge-key"{key_title}>🌟 關鍵分點</span>'
            if getattr(br, "is_key_broker", False)
            else ""
        )
        ht_badge = (
            f'<span class="badge badge-ht" title="{_esc("、".join(br.turnover_reasons))}">⚡高周轉/避險</span>'
            if br.is_high_turnover
            else ""
        )
        key_metrics_parts: list[str] = []
        early_ratio = getattr(br, "early_accum_ratio", None)
        win_rate = getattr(br, "buy_5d_win_rate", None)
        win_samples = getattr(br, "buy_5d_sample_count", 0) or 0
        if early_ratio is None:
            key_metrics_parts.append("領先建倉：波段過短不計")
        elif early_ratio > 0:
            key_metrics_parts.append(f"領先建倉 {early_ratio:.0f}%")
        if win_rate is not None and win_samples > 0:
            key_metrics_parts.append(f"買後5日勝率 {win_rate:.0f}% ({win_samples}次)")
        key_metrics_html = (
            f'<div class="sub-pct">{" · ".join(key_metrics_parts)}</div>'
            if key_metrics_parts
            else ""
        )
        bar_pct = min(100.0, max(4.0, br.wave_net_lots / max(max_holding, 1) * 100.0))

        profit_cls = _signed_cls(br.profit_pct)
        tier_tag = _render_profit_tier_tag(br.profit_tier)
        vs_cost_txt = (
            f'<div class="sub-pct">個股均價 {today_avg_price:,.1f} ({br.today_price_vs_cost_pct:+.1f}%)</div>'
        )

        vol_pct_str = f"量 {br.today_net_pct_of_volume:+.1f}%"
        if br.today_net_lots > 0:
            pct_str = (
                f"🆕 新進場 · {vol_pct_str}"
                if br.is_new_position or br.today_net_pct_of_holding is None
                else f"持股 {br.today_net_pct_of_holding:+.1f}% · {vol_pct_str}"
            )
            today_action_html = (
                f'<span class="num-pos">買超 +{br.today_net_lots:,}</span>'
                f'<div class="sub-pct">{pct_str}</div>'
            )
        elif br.today_net_lots < 0:
            pct_str = (
                f"🆕 新部位 · {vol_pct_str}"
                if br.is_new_position or br.today_net_pct_of_holding is None
                else f"持股 {br.today_net_pct_of_holding:+.1f}% · {vol_pct_str}"
            )
            today_action_html = (
                f'<span class="num-neg">賣超 {br.today_net_lots:,}</span>'
                f'<div class="sub-pct">{pct_str}</div>'
            )
        else:
            today_action_html = '<span class="num-zero">0 (0.0%)</span>'

        signal_parts: list[str] = []
        for a in br.sell_alerts:
            signal_parts.append(f'<div class="cell-alert">🚨 {_esc(a)}</div>')
        for ba in br.buy_alerts:
            signal_parts.append(f'<div class="cell-buy">📈 {_esc(ba)}</div>')
        if br.is_high_turnover and not signal_parts:
            alert_cell = '<span class="cell-ht">⚡ 高周轉/避險（不計警示）</span>'
        elif signal_parts:
            alert_cell = "".join(signal_parts)
        else:
            alert_cell = '<span class="cell-ok">正常</span>'

        if br.consecutive_net_sell_days > 0:
            consec_html = (
                f'<span class="num-neg">連賣 {br.consecutive_net_sell_days} 天</span>'
                f'<div class="sub-pct">(-{br.consecutive_net_sell_lots:,}張)</div>'
            )
        elif br.consecutive_net_buy_days > 0:
            consec_html = (
                f'<span class="num-pos">連買 {br.consecutive_net_buy_days} 天</span>'
                f'<div class="sub-pct">(+{br.consecutive_net_buy_lots:,}張)</div>'
            )
        else:
            consec_html = "-"

        rows_html.append(
            f"""
            <tr class="{'row-alert' if br.sell_alerts else ''}">
              <td class="col-center"><strong>{br.rank}</strong></td>
              <td><div class="broker-name">{_esc(br.name)} {key_badge} {geo_badge} {ht_badge}</div>{key_metrics_html}</td>
              <td>{_render_period_badge(br.period_tags, br.period_count)}</td>
              <td class="col-right">
                <strong class="hold-num">{br.wave_net_lots:,}</strong>
                <div class="bar-track"><div class="bar-fill" style="width:{bar_pct:.1f}%"></div></div>
              </td>
              <td class="col-center">{_render_sparkline(br.last_10d_holdings, br.last_10d_change)}</td>
              <td class="col-right">
                <div>{br.avg_cost:,.2f}</div>
                {vs_cost_txt}
              </td>
              <td class="col-right">
                <span class="{profit_cls}">{br.profit_pct:+.1f}%</span>
                {tier_tag}
              </td>
              <td class="col-right num-pos">{br.today_buy_lots:,}</td>
              <td class="col-right num-neg">{br.today_sell_lots:,}</td>
              <td class="col-right">{today_action_html}</td>
              <td class="col-center">{consec_html}</td>
              <td>{alert_cell}</td>
            </tr>
            """
        )
    return "".join(rows_html)


def generate_html_report(
    results: list[StockChipMonitorResult],
    out_dir: Path,
    top_n: int = 5,
) -> tuple[Path, Path]:
    if not results:
        raise ValueError("沒有可產生 HTML 報表的監控結果")

    out_dir.mkdir(parents=True, exist_ok=True)
    report_date = results[0].data_date
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total_stocks = len(results)
    alert_stocks = sum(1 for r in results if r.alerts)
    foreign_alert_stocks = sum(1 for r in results if r.foreign_alerts)
    buy_alert_stocks = sum(1 for r in results if r.overall_status in ("buy", "divergent"))
    margin_published_any = any(r.chip_flow and r.chip_flow.margin_available for r in results)
    retail_shift_stocks = sum(1 for r in results if r.is_chips_to_retail)

    tab_buttons: list[str] = [
        '<button type="button" class="tab-btn active" data-tab="overview">📊 綜合資訊</button>'
    ]
    overview_rows: list[str] = []
    for r in results:
        if r.overall_status == "divergent":
            pill_cls, icon, status_label = "tab-div", "⚖️", '<span class="status-pill status-div">⚖️ 分歧</span>'
        elif r.overall_status == "sell":
            pill_cls, icon, status_label = "tab-alert", "🚨", '<span class="status-pill status-danger">🚨 賣出警示</span>'
        elif r.overall_status == "buy":
            pill_cls, icon, status_label = "tab-buy", "📈", '<span class="status-pill status-buy">📈 建倉加碼</span>'
        else:
            pill_cls, icon, status_label = "tab-ok", "✅", '<span class="status-pill status-ok">✅ 穩定</span>'

        tab_buttons.append(
            f'<button type="button" class="tab-btn {pill_cls}" data-tab="stock-{_esc(r.stock_id)}">{icon} {_esc(r.stock_id)} {_esc(r.stock_name)}</button>'
        )

        w = r.wave_info
        cf = r.chip_flow
        foreign_net = cf.foreign_chg if cf is not None else sum(b.today_net_lots for b in r.foreign_branches)
        dom_net = sum(b.today_net_lots for b in r.top_branches)
        vol = w.today_volume_lots
        f_vol_pct = f" ({abs(foreign_net) / vol * 100:.1f}%)" if vol > 0 and foreign_net != 0 else ""

        key_names = [
            f'<span class="badge badge-key" title="{_esc("、".join(getattr(b, "key_broker_reasons", []) or []))}">🌟 {_esc(b.name)}</span>'
            for b in (r.top_branches + r.foreign_branches)
            if getattr(b, "is_key_broker", False)
        ]
        key_cell = " ".join(key_names[:2]) if key_names else '<span class="num-zero">-</span>'
        if len(key_names) > 2:
            key_cell += f' <span class="sub-pct">+{len(key_names) - 2}家</span>'

        if cf is not None and cf.margin_available:
            margin_cell = f'<span class="{_signed_cls(cf.margin_chg)}">{cf.margin_chg:+,}</span>'
        else:
            margin_cell = '<span class="num-zero">待更新</span>'

        retail_flag = ' <span class="status-pill status-warn">⚠️ 轉散戶</span>' if r.is_chips_to_retail else ""
        stance_lbl, stance_cls, stance_reason = _derive_action_stance(r)

        margin_sort_val = cf.margin_chg if (cf is not None and cf.margin_available) else -999999999
        overview_rows.append(
            f"""
            <tr class="overview-row" data-goto="stock-{_esc(r.stock_id)}" data-stock="{_esc(r.stock_id)}" data-chg="{w.today_change_pct:.4f}" data-vol="{vol}" data-foreign="{foreign_net}" data-dom="{dom_net}" data-margin="{margin_sort_val}">
              <td>
                <button type="button" class="overview-link" data-goto="stock-{_esc(r.stock_id)}">
                  <strong>{_esc(r.stock_id)} {_esc(r.stock_name)}</strong>
                </button>
              </td>
              <td class="col-right">
                <strong>{w.latest_close:,.2f}</strong>
                <div class="sub-pct {_signed_cls(w.today_change_pct)}">{w.today_change_pct:+.2f}%</div>
              </td>
              <td class="col-right">{vol:,}</td>
              <td>{status_label}{retail_flag}</td>
              <td>
                <span class="stance-pill {stance_cls}">{_esc(stance_lbl)}</span>
                <div class="sub-pct">{_esc(stance_reason)}</div>
              </td>
              <td class="col-right"><span class="{_signed_cls(foreign_net)}">{foreign_net:+,}</span><div class="sub-pct">佔量{f_vol_pct or ' 0%'}</div></td>
              <td class="col-right"><span class="{_signed_cls(dom_net)}">{dom_net:+,}</span></td>
              <td class="col-center">{margin_cell}</td>
              <td>{key_cell}</td>
            </tr>
            """
        )

    alert_blocks: list[str] = []
    all_sell_items = [
        f'<li><strong>{_esc(r.stock_id)} {_esc(r.stock_name)}</strong>：{_esc(al)}</li>'
        for r in results
        for al in r.alerts
    ]
    if all_sell_items:
        alert_blocks.append(f"""
        <div class="global-alert alert-danger">
          <div class="global-alert-title">🚨 本土主力大單賣出警示總覽（共 {len(all_sell_items)} 項）</div>
          <ul>{''.join(all_sell_items)}</ul>
        </div>
        """)

    all_foreign_items = [
        f'<li><strong>{_esc(r.stock_id)} {_esc(r.stock_name)}</strong>：{_esc(fal)}</li>'
        for r in results
        for fal in r.foreign_alerts
    ]
    if all_foreign_items:
        alert_blocks.append(f"""
        <div class="global-alert alert-foreign">
          <div class="global-alert-title">🌐 外資分點賣出與籌碼流向警示（共 {len(all_foreign_items)} 項）</div>
          <ul>{''.join(all_foreign_items)}</ul>
        </div>
        """)

    all_buy_items = [
        f'<li><strong>{_esc(r.stock_id)} {_esc(r.stock_name)}</strong>：{_esc(bal)}</li>'
        for r in results
        for bal in (r.buy_alerts + r.foreign_buy_alerts)
    ]
    if all_buy_items:
        alert_blocks.append(f"""
        <div class="global-alert alert-buy">
          <div class="global-alert-title">📈 主力持續建倉 / 加碼通知（共 {len(all_buy_items)} 項）</div>
          <ul>{''.join(all_buy_items)}</ul>
        </div>
        """)

    if not alert_blocks:
        alert_blocks.append(f"""
        <div class="global-alert alert-safe">
          <div class="global-alert-title">✅ 今日籌碼監控總覽</div>
          <p>所有監控標的今日前{top_n}大本土主力與外資分點皆無異常大單賣出或重大建倉訊號。</p>
        </div>
        """)
    global_alert_html = "\n".join(alert_blocks)

    table_head = """
      <thead>
        <tr>
          <th class="col-center">名次</th>
          <th>主力分點</th>
          <th>多週期交叉</th>
          <th class="col-right">目前持有(張)</th>
          <th class="col-center">近10日走勢</th>
          <th class="col-right">FIFO成本 / 個股均價</th>
          <th class="col-right">較成本高</th>
          <th class="col-right">今日買(張)</th>
          <th class="col-right">今日賣(張)</th>
          <th class="col-right">今日淨買賣 (佔異動前%)</th>
          <th class="col-center">連買/連賣</th>
          <th>買賣/建倉訊號</th>
        </tr>
      </thead>
    """

    cards_html: list[str] = []
    for r in results:
        w = r.wave_info
        all_branches_today = r.top_branches + r.foreign_branches
        day_buy_sum = sum(b.today_net_lots for b in all_branches_today if b.today_net_lots > 0)
        day_sell_sum = sum(b.today_net_lots for b in all_branches_today if b.today_net_lots < 0)

        status_badges: list[str] = []
        if r.overall_status == "divergent":
            status_badges.append(
                f'<span class="status-pill status-div">⚖️ 主力分歧（買方 +{day_buy_sum:,} 張 / 賣方 {day_sell_sum:,} 張）</span>'
            )
        elif r.overall_status == "sell":
            status_badges.append('<span class="status-pill status-danger">🚨 大單賣出警示</span>')
        elif r.overall_status == "buy":
            status_badges.append('<span class="status-pill status-buy">📈 主力持續建倉加碼</span>')
        else:
            status_badges.append('<span class="status-pill status-ok">✅ 主力籌碼穩定</span>')

        if r.is_chips_to_retail:
            status_badges.append('<span class="status-pill status-warn">⚠️ 籌碼轉向散戶（外資賣＋融資增）</span>')
        if not r.has_clear_domestic_main_force:
            status_badges.append('<span class="status-pill status-muted">⚠️ 無明確本土主力（單家皆&lt;股本0.3%）</span>')
        if r.has_resonance_sell:
            status_badges.append(
                f'<span class="status-pill status-danger">共振出貨({r.top3_net_sell_count}家)</span>'
            )
        if r.has_resonance_buy:
            status_badges.append(
                f'<span class="status-pill status-buy">🔥 共振加碼({r.top3_net_buy_count}家)</span>'
            )
        stance_lbl, stance_cls, stance_reason = _derive_action_stance(r)
        status_badges.append(
            f'<span class="stance-pill {stance_cls}" title="{_esc(stance_reason)}">建議：{_esc(stance_lbl)}（{_esc(stance_reason)}）</span>'
        )

        today_chg_cls = _signed_cls(w.today_change_pct)
        gain_cls = _signed_cls(w.gain_from_trough_pct)
        cap_str = (
            f" ｜ 佔股本 <strong>{r.concentration_capital_pct:.2f}%</strong>"
            if r.concentration_capital_pct > 0
            else ""
        )

        flow_parts: list[str] = []
        if r.chip_flow:
            cf = r.chip_flow
            f_cls = _signed_cls(cf.foreign_chg)
            t_cls = _signed_cls(cf.trust_chg)
            if cf.margin_available:
                m_cls = _signed_cls(cf.margin_chg)
                margin_txt = f'<strong>{cf.margin_balance:,} 張</strong> (<span class="{m_cls}">{cf.margin_chg:+,}</span>, 佔股本 {cf.margin_pct_capital:.2f}%)'
            else:
                prev_m = (
                    f'｜前日 {cf.margin_latest_date}: <span class="{_signed_cls(cf.margin_latest_chg)}">{cf.margin_latest_chg:+,}</span>'
                    if cf.margin_latest_date
                    else ""
                )
                margin_txt = f'<strong>{cf.margin_balance:,} 張</strong> (<span class="num-zero">今日尚未公布</span>{prev_m})'
            short_txt = (
                f'<strong>{cf.short_balance:,} 張</strong> ({cf.short_chg:+,})'
                if cf.short_available
                else f'<strong>{cf.short_balance:,} 張</strong> (今日尚未公布)'
            )
            flow_parts.append(f'<span>💳 融資：{margin_txt}</span>')
            flow_parts.append(
                f'<span>🏦 三大法人當日：外資 <span class="{f_cls}">{cf.foreign_chg:+,}</span> ｜ 投信 <span class="{t_cls}">{cf.trust_chg:+,}</span> ｜ 自營 {cf.dealer_chg:+,}</span>'
            )
            flow_parts.append(f'<span>🔒 融券：{short_txt}</span>')

        if r.weekly_big_holders:
            bh = r.weekly_big_holders
            bh_cls = _signed_cls(bh.big_holder_diff)
            flow_parts.append(
                f'<span>🏛️ 集保大戶({_esc(bh.latest_date)})：<strong>{bh.big_holder_pct:.2f}%</strong> (<span class="{bh_cls}">週變 {bh.big_holder_diff:+.2f}%</span>) ｜ 集中度 {bh.concentration_pct:.2f}% ({bh.concentration_diff:+.2f}%)</span>'
            )

        flow_html = (
            f'<div class="card-meta" style="margin-top:4px;">{"".join(flow_parts)}</div>'
            if flow_parts
            else ""
        )

        foreign_html = ""
        if r.foreign_branches:
            f_rows = _render_branch_rows(r.foreign_branches, w.today_avg_price)
            foreign_html = f"""
            <details class="foreign-box" open>
              <summary>🌐 外資分點觀察區（共 {len(r.foreign_branches)} 家，不列入隔日沖排除，警示已計入總覽）</summary>
              <div class="table-wrap">
                <table class="branch-table">
                  {table_head}
                  <tbody>{f_rows}</tbody>
                </table>
              </div>
            </details>
            """

        if r.excluded_day_traders:
            ex_items = "".join(
                f"<li><strong>{_esc(ex.name)}</strong>（區間淨買 {ex.wave_net_lots:,} 張）：{_esc('、'.join(ex.eval_info.reasons))}</li>"
                for ex in r.excluded_day_traders
            )
            excluded_html = f"""
            <details class="excluded-box">
              <summary>已依個股行為排除 {len(r.excluded_day_traders)} 家本土隔日沖分點（點擊展開明細）</summary>
              <ul>{ex_items}</ul>
            </details>
            """
        else:
            excluded_html = '<div class="excluded-none">此波段本土買超分點中無符合隔日沖行為者。</div>'

        top_sellers = getattr(r, "top_sellers", []) or []
        sellers_html = ""
        if top_sellers:
            s_rows = "".join(
                f"""
                <tr>
                  <td class="col-center"><strong>{s.rank}</strong></td>
                  <td><div class="broker-name">{_esc(s.name)}{' <span class="badge badge-geo">外資</span>' if s.is_foreign else ''}</div></td>
                  <td class="col-right num-pos">{s.buy_lots:,}</td>
                  <td class="col-right num-neg">{s.sell_lots:,}</td>
                  <td class="col-right"><span class="num-neg">{s.net_lots:,}</span></td>
                  <td class="col-right">{s.volume_pct:.1f}%</td>
                </tr>
                """
                for s in top_sellers
            )
            trust_dealer_sell = (
                r.chip_flow is not None
                and w.today_volume_lots > 0
                and (r.chip_flow.trust_chg + r.chip_flow.dealer_chg) <= -max(50, int(w.today_volume_lots * 0.05))
            )
            open_attr = " open" if trust_dealer_sell else ""
            sellers_html = f"""
            <details class="sellers-box"{open_attr}>
              <summary>📉 今日前 {len(top_sellers)} 大賣超分點（補齊賣方明細，避免只見買盤）</summary>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th class="col-center">名次</th>
                      <th>賣超分點</th>
                      <th class="col-right">今日買(張)</th>
                      <th class="col-right">今日賣(張)</th>
                      <th class="col-right">今日淨賣超(張)</th>
                      <th class="col-right">佔當日量</th>
                    </tr>
                  </thead>
                  <tbody>{s_rows}</tbody>
                </table>
              </div>
            </details>
            """

        cards_html.append(
            f"""
            <div class="tab-panel" id="tab-stock-{_esc(r.stock_id)}">
              <section class="stock-card" id="stock-{_esc(r.stock_id)}">
                <div class="card-header">
                  <div class="card-title-row">
                    <h2>{_esc(r.stock_id)} {_esc(r.stock_name)}
                      <span class="today-quote">收盤 <strong>{w.latest_close:,.2f}</strong> (<span class="{today_chg_cls}">{w.today_change_pct:+.2f}%</span>) ｜ 量 <strong>{w.today_volume_lots:,} 張</strong> ｜ 個股均價 {w.today_avg_price:,.2f}</span>
                    </h2>
                    <div class="status-group">{' '.join(status_badges)}</div>
                  </div>
                  <div class="card-meta">
                    <span>📅 資料日：<strong>{_esc(r.data_date)}</strong></span>
                    <span>📈 波段低點：<strong>{_esc(w.trough_date)}</strong> ({w.trough_price:,.2f}) → 累計 <span class="{gain_cls}">{w.gain_from_trough_pct:+.2f}%</span></span>
                    <span>🔍 整理期觀察起點：<strong>{_esc(w.observation_start)}</strong></span>
                    <span>🏦 前{top_n}大本土主力持有(依持股排序)：<strong class="hold-total">{r.top_branches_total_lots:,} 張</strong>（佔波段成交 <strong>{r.concentration_wave_vol_pct:.2f}%</strong>{cap_str}）</span>
                  </div>
                  {flow_html}
                </div>
                <div class="table-wrap">
                  <table class="branch-table">
                    {table_head}
                    <tbody>
                      {_render_branch_rows(r.top_branches, w.today_avg_price)}
                    </tbody>
                  </table>
                </div>
                {sellers_html}
                {foreign_html}
                {excluded_html}
              </section>
            </div>
            """
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>台股每日主力籌碼分點監控報表 ({_esc(report_date)})</title>
  <style>
    :root {{
      --bg: #f4f6f9;
      --card: #ffffff;
      --text: #1e293b;
      --muted: #64748b;
      --border: #e2e8f0;
      --red: #dc2626;
      --green: #16a34a;
      --amber: #d97706;
      --blue: #2563eb;
      --purple: #7c3aed;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 20px;
      font-family: "Microsoft JhengHei", "Noto Sans TC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    .container {{ max-width: 1460px; margin: 0 auto; }}
    .page-header {{ margin-bottom: 18px; }}
    .page-header h1 {{ margin: 0; font-size: 23px; }}
    .page-subtitle {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .kpi-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 13px 15px;
      box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    }}
    .kpi-label {{ font-size: 12px; color: var(--muted); }}
    .kpi-value {{ font-size: 22px; font-weight: 700; margin-top: 3px; }}
    .kpi-danger {{ color: var(--red); }}
    .kpi-warn {{ color: var(--amber); }}
    .kpi-ok {{ color: var(--green); }}
    .global-alert {{ border-radius: 10px; padding: 13px 18px; margin-bottom: 12px; border: 1px solid; }}
    .alert-danger {{ background: #fef2f2; border-color: #fecaca; color: #991b1b; }}
    .alert-foreign {{ background: #fffbeb; border-color: #fde68a; color: #92400e; }}
    .alert-buy {{ background: #eff6ff; border-color: #bfdbfe; color: #1e40af; }}
    .alert-safe {{ background: #f0fdf4; border-color: #bbf7d0; color: #166534; }}
    .global-alert-title {{ font-weight: 700; font-size: 14.5px; margin-bottom: 4px; }}
    .global-alert ul {{ margin: 6px 0 0 20px; padding: 0; font-size: 13.5px; }}
    .global-alert p {{ margin: 4px 0 0; }}
    .tab-bar {{
      position: sticky;
      top: 0;
      z-index: 20;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 10px 12px;
      margin-bottom: 16px;
      background: rgba(244, 246, 249, 0.94);
      backdrop-filter: blur(6px);
      border: 1px solid var(--border);
      border-radius: 12px;
    }}
    .tab-btn {{
      cursor: pointer;
      padding: 7px 14px;
      border-radius: 999px;
      font-size: 13.5px;
      font-weight: 700;
      font-family: inherit;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      transition: all 0.15s ease;
    }}
    .tab-btn:hover {{ border-color: #94a3b8; transform: translateY(-1px); }}
    .tab-btn.tab-alert {{ border-color: #fca5a5; background: #fef2f2; color: #b91c1c; }}
    .tab-btn.tab-div {{ border-color: #fcd34d; background: #fffbeb; color: #92400e; }}
    .tab-btn.tab-buy {{ border-color: #93c5fd; background: #eff6ff; color: #1d4ed8; }}
    .tab-btn.active {{
      background: #0f172a;
      border-color: #0f172a;
      color: #ffffff;
      box-shadow: 0 2px 6px rgba(15, 23, 42, 0.18);
    }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    .overview-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      margin-bottom: 16px;
      box-shadow: 0 2px 6px rgba(15, 23, 42, 0.04);
      overflow: hidden;
    }}
    .overview-card-header {{
      padding: 12px 18px;
      border-bottom: 1px solid var(--border);
      background: #f8fafc;
      font-weight: 700;
      font-size: 15px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .overview-hint {{ font-size: 12px; font-weight: 500; color: var(--muted); }}
    .overview-row {{ cursor: pointer; transition: background 0.12s ease; }}
    .overview-row:hover {{ background: #f8fafc; }}
    .overview-link {{
      background: none;
      border: none;
      padding: 0;
      font: inherit;
      color: #1d4ed8;
      cursor: pointer;
      text-align: left;
    }}
    .overview-link:hover {{ text-decoration: underline; }}
    .stock-card {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; margin-bottom: 22px;
      box-shadow: 0 2px 6px rgba(15, 23, 42, 0.04); overflow: hidden;
    }}
    .card-header {{ padding: 14px 18px; border-bottom: 1px solid var(--border); background: #f8fafc; }}
    .card-title-row {{
      display: flex; flex-wrap: wrap; justify-content: space-between;
      align-items: center; gap: 10px; margin-bottom: 6px;
    }}
    .card-title-row h2 {{ margin: 0; font-size: 19px; color: #0f172a; }}
    .today-quote {{ font-size: 13.5px; font-weight: 500; color: #334155; margin-left: 10px; }}
    .status-group {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .status-pill {{ padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .status-danger {{ background: #fee2e2; color: #b91c1c; }}
    .status-div {{ background: #fef3c7; color: #92400e; }}
    .status-buy {{ background: #dbeafe; color: #1d4ed8; }}
    .status-warn {{ background: #ffedd5; color: #9a3412; }}
    .status-muted {{ background: #f1f5f9; color: #475569; }}
    .status-ok {{ background: #dcfce7; color: #166534; }}
    .stance-pill {{ display: inline-block; padding: 3px 9px; border-radius: 6px; font-size: 12px; font-weight: 800; }}
    .stance-bull {{ background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; }}
    .stance-neutral {{ background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; }}
    .stance-bear {{ background: #dcfce7; color: #15803d; border: 1px solid #86efac; }}
    .card-meta {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 12.5px; color: #475569; }}
    .hold-total {{ color: var(--purple); }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
    th {{ background: #f8fafc; color: #475569; font-weight: 700; white-space: nowrap; text-align: left; }}
    .col-right {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .col-center {{ text-align: center; }}
    .row-alert {{ background: #fff7f7; }}
    .broker-name {{ font-weight: 700; font-size: 13.5px; white-space: nowrap; }}
    .hold-num {{ color: var(--purple); font-size: 14.5px; }}
    .bar-track {{ width: 82px; height: 5px; background: #e2e8f0; border-radius: 3px; margin-left: auto; margin-top: 4px; }}
    .bar-fill {{ height: 100%; background: var(--purple); border-radius: 3px; }}
    .num-pos {{ color: var(--red); font-weight: 700; }}
    .num-neg {{ color: var(--green); font-weight: 700; }}
    .num-zero {{ color: var(--muted); }}
    .sub-pct {{ font-size: 11px; color: var(--muted); white-space: nowrap; }}
    .badge {{ display: inline-block; padding: 2px 6px; border-radius: 5px; font-size: 11px; font-weight: 700; }}
    .badge-star {{ background: #fee2e2; color: #b91c1c; }}
    .badge-mid {{ background: #fef3c7; color: #92400e; }}
    .badge-short {{ background: #f1f5f9; color: #475569; }}
    .badge-geo {{ background: #dbeafe; color: #1d4ed8; margin-left: 4px; }}
    .badge-key {{ background: #fef3c7; color: #b45309; margin-left: 4px; border: 1px solid #fcd34d; }}
    .badge-ht {{ background: #f1f5f9; color: #64748b; margin-left: 4px; border: 1px solid #cbd5e1; }}
    .tag-row {{ margin-top: 3px; display: flex; flex-wrap: wrap; gap: 3px; }}
    .tag-mini {{ background: #f1f5f9; color: #475569; font-size: 10.5px; padding: 1px 5px; border-radius: 4px; }}
    .tier-tag {{ font-size: 11px; font-weight: 700; margin-top: 2px; white-space: nowrap; }}
    .tier-doubled {{ color: #991b1b; }}
    .tier-high {{ color: #dc2626; }}
    .tier-warn {{ color: #d97706; }}
    .tier-trapped {{ color: #0284c7; }}
    .cell-alert {{ color: #b91c1c; font-weight: 700; font-size: 12px; }}
    .cell-buy {{ color: #1d4ed8; font-weight: 700; font-size: 12px; }}
    .cell-ht {{ color: #64748b; font-size: 12px; }}
    .cell-ok {{ color: var(--green); font-weight: 600; }}
    .foreign-box {{ border-top: 2px solid #e2e8f0; background: #f8fafc; }}
    .foreign-box summary {{ padding: 9px 18px; cursor: pointer; font-weight: 700; font-size: 13px; color: #1e40af; }}
    .sellers-box {{ border-top: 2px solid #fecaca; background: #fffafb; }}
    .sellers-box summary {{ padding: 9px 18px; cursor: pointer; font-weight: 700; font-size: 13px; color: #b91c1c; }}
    .branch-table th:nth-child(1),
    .branch-table td:nth-child(1) {{
      position: sticky; left: 0; z-index: 2; width: 48px; min-width: 48px;
    }}
    .branch-table th:nth-child(2),
    .branch-table td:nth-child(2) {{
      position: sticky; left: 48px; z-index: 2;
      box-shadow: 2px 0 5px rgba(15, 23, 42, 0.06);
    }}
    .branch-table th:nth-child(1),
    .branch-table th:nth-child(2) {{ background: #f8fafc; z-index: 3; }}
    .branch-table td:nth-child(1),
    .branch-table td:nth-child(2) {{ background: #ffffff; }}
    .branch-table tr.row-alert td:nth-child(1),
    .branch-table tr.row-alert td:nth-child(2) {{ background: #fff7f7; }}
    th.sortable {{ cursor: pointer; user-select: none; }}
    th.sortable:hover {{ background: #e2e8f0; }}
    th.sortable::after {{ content: " ⇅"; font-size: 10px; color: #94a3b8; }}
    th.sortable.sort-asc::after {{ content: " ▲"; color: #1d4ed8; }}
    th.sortable.sort-desc::after {{ content: " ▼"; color: #1d4ed8; }}
    .excluded-box {{ padding: 9px 18px; background: #fafafa; font-size: 12px; color: var(--muted); }}
    .excluded-box summary {{ cursor: pointer; font-weight: 600; }}
    .excluded-box ul {{ margin: 6px 0 2px 18px; padding: 0; }}
    .excluded-none {{ padding: 9px 18px; font-size: 12px; color: var(--muted); background: #fafafa; }}
    .legend {{ font-size: 12px; color: var(--muted); margin-top: 8px; text-align: right; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="page-header">
      <h1>台股每日主力籌碼分點監控報表（前{top_n}大本土主力 ＋ 外資觀察區）</h1>
      <div class="page-subtitle">資料日期：{_esc(report_date)} ｜ 報表產生時間：{_esc(generated_at)} ｜ 紅字買超/上漲、綠字賣超/下跌 ｜ 名次依目前持有張數排序</div>
    </div>

    <div class="tab-bar" role="tablist">{''.join(tab_buttons)}</div>

    <div class="tab-panel active" id="tab-overview">
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label">監控股票總數</div>
          <div class="kpi-value">{total_stocks} 檔</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">本土大單賣出警示</div>
          <div class="kpi-value {'kpi-danger' if alert_stocks > 0 else 'kpi-ok'}">{alert_stocks} 檔</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">外資分點賣出警示</div>
          <div class="kpi-value {'kpi-warn' if foreign_alert_stocks > 0 else 'kpi-ok'}">{foreign_alert_stocks} 檔</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">主力持續建倉加碼</div>
          <div class="kpi-value" style="color: var(--blue);">{buy_alert_stocks} 檔</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">籌碼轉向散戶 (外資賣+融資增)</div>
          <div class="kpi-value {'kpi-danger' if retail_shift_stocks > 0 else ('kpi-ok' if margin_published_any else '')}">{f'{retail_shift_stocks} 檔' if margin_published_any else '融資尚未公布'}</div>
        </div>
      </div>

      <div class="overview-card">
        <div class="overview-card-header">
          <span>📋 各股籌碼綜合一覽</span>
          <span class="overview-hint">點擊任一列或上方分頁標籤切換至該股明細</span>
        </div>
        <div class="table-wrap">
          <table id="overview-table">
            <thead>
              <tr>
                <th class="sortable" data-sort="stock">股票</th>
                <th class="col-right sortable" data-sort="chg">收盤 / 漲跌</th>
                <th class="col-right sortable" data-sort="vol">成交量(張)</th>
                <th>狀態</th>
                <th>操作建議</th>
                <th class="col-right sortable" data-sort="foreign">外資今日(張)</th>
                <th class="col-right sortable" data-sort="dom">本土前{top_n}今日(張)</th>
                <th class="col-center sortable" data-sort="margin">融資變動</th>
                <th>關鍵分點</th>
              </tr>
            </thead>
            <tbody>{''.join(overview_rows)}</tbody>
          </table>
        </div>
      </div>

      {global_alert_html}
    </div>

    {''.join(cards_html)}

    <div class="legend">
      台股每日主力籌碼監控系統 ｜ 連買連賣嚴格依市場交易日對齊，今日無交易即中斷
    </div>
  </div>
  <script>
    (function () {{
      var buttons = document.querySelectorAll('.tab-btn');
      var panels = document.querySelectorAll('.tab-panel');

      function activateTab(tabId, skipHash) {{
        var targetPanel = document.getElementById('tab-' + tabId);
        if (!targetPanel) return;
        buttons.forEach(function (btn) {{
          btn.classList.toggle('active', btn.getAttribute('data-tab') === tabId);
        }});
        panels.forEach(function (panel) {{
          panel.classList.toggle('active', panel.id === 'tab-' + tabId);
        }});
        if (!skipHash && window.history && window.history.replaceState) {{
          window.history.replaceState(null, '', '#' + tabId);
        }}
        window.scrollTo({{ top: 0, behavior: 'smooth' }});
      }}

      buttons.forEach(function (btn) {{
        btn.addEventListener('click', function () {{
          activateTab(btn.getAttribute('data-tab'));
        }});
      }});

      document.querySelectorAll('[data-goto]').forEach(function (el) {{
        el.addEventListener('click', function () {{
          activateTab(el.getAttribute('data-goto'));
        }});
      }});

      var overviewTable = document.getElementById('overview-table');
      if (overviewTable) {{
        var headers = overviewTable.querySelectorAll('th.sortable');
        var sortState = {{ key: null, dir: -1 }};
        headers.forEach(function (th) {{
          th.addEventListener('click', function () {{
            var key = th.getAttribute('data-sort');
            sortState.dir = (sortState.key === key) ? -sortState.dir : -1;
            sortState.key = key;
            headers.forEach(function (h) {{ h.classList.remove('sort-asc', 'sort-desc'); }});
            th.classList.add(sortState.dir === 1 ? 'sort-asc' : 'sort-desc');
            var tbody = overviewTable.querySelector('tbody');
            var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
            rows.sort(function (a, b) {{
              var av = a.getAttribute('data-' + key) || '';
              var bv = b.getAttribute('data-' + key) || '';
              if (key === 'stock') {{
                return av.localeCompare(bv) * sortState.dir;
              }}
              return (parseFloat(av) - parseFloat(bv)) * sortState.dir;
            }});
            rows.forEach(function (row) {{ tbody.appendChild(row); }});
          }});
        }});
      }}

      if (window.location.hash && window.location.hash.length > 1) {{
        activateTab(window.location.hash.slice(1), true);
      }}
    }})();
  </script>
</body>
</html>
"""

    dated_path = out_dir / f"chip_report_{report_date}.html"
    latest_path = out_dir / "latest_report.html"
    dated_path.write_text(html_doc, encoding="utf-8")
    latest_path.write_text(html_doc, encoding="utf-8")
    return dated_path, latest_path
