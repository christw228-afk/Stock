from __future__ import annotations

import argparse
import csv
import html
import json
import time
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any

import schedule
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from html_report import generate_html_report
from monitor import StockChipMonitorResult, analyze_stock
from notifier import send_telegram_document, send_telegram_message

console = Console()


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {
            "defaults": {
                "wave_search_days": 120,
                "pre_wave_days": 30,
                "top_n_branches": 3,
                "geo_boost_default": 1.35,
                "geo_boost_small_cap": 1.5,
                "profit_risk_pct": 30.0,
            },
            "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
            "geo_brokers": {},
            "watchlist": [],
        }
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_watchlist_csv(csv_path: Path) -> list[dict[str, Any]]:
    """Load watchlist from CSV (supports UTF-8-SIG, CP950/Big5, with or without header)."""
    raw = csv_path.read_bytes()
    text: str | None = None
    for enc in ("utf-8-sig", "cp950", "big5", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"無法辨識 CSV 編碼: {csv_path}")

    items: list[dict[str, Any]] = []
    reader = csv.reader(text.splitlines())
    for row in reader:
        cols = [c.strip() for c in row]
        if not cols or not cols[0]:
            continue
        sid = cols[0]
        # Skip optional header row
        if sid.lower() in ("stock_id", "股票代號", "代號", "code"):
            continue
        entry: dict[str, Any] = {"stock_id": sid}
        if len(cols) >= 2 and cols[1]:
            entry["name"] = cols[1]
        if len(cols) >= 3 and cols[2]:
            entry["wave_start"] = cols[2]
        if len(cols) >= 4 and cols[3].lower() in ("1", "true", "y", "yes", "是"):
            entry["small_cap"] = True
        items.append(entry)
    return items


def _format_today_action(net_lots: int, net_pct: float | None, is_new: bool = False) -> str:
    if net_lots > 0:
        pct_s = "🆕 新進場" if is_new or net_pct is None else f"({net_pct:+.1f}%)"
        return f"[red]買超 +{net_lots:,} {pct_s}[/red]"
    if net_lots < 0:
        pct_s = "🆕 新部位" if is_new or net_pct is None else f"({net_pct:+.1f}%)"
        return f"[green]賣超 {net_lots:,} {pct_s}[/green]"
    return "0 (0.0%)"


def _format_period_badge(tags: list[str], count: int) -> str:
    if count >= 3:
        return f"[bold red]★三週期共振[/bold red]\n[dim]{' '.join(tags)}[/dim]"
    if count == 2:
        return f"[yellow]雙週期[/yellow]\n[dim]{' '.join(tags)}[/dim]"
    if tags == ["20日#1"] or (count == 1 and any(t.startswith("20日") for t in tags)):
        return f"[dim]僅20日短線[/dim]\n[dim]{' '.join(tags)}[/dim]"
    if tags:
        return f"[dim]{' '.join(tags)}[/dim]"
    return "[dim]波段區間[/dim]"


def render_result(res: StockChipMonitorResult, top_n: int = 5) -> None:
    w = res.wave_info
    status_badge = (
        "[bold red]🚨 觸發大單賣出警示[/bold red]"
        if res.should_notify_telegram
        else "[bold green]✅ 主力籌碼穩定[/bold green]"
    )

    header_text = (
        f"[bold cyan]{res.stock_id} {res.stock_name}[/bold cyan]  |  "
        f"資料日: [bold]{res.data_date}[/bold]  |  {status_badge}\n"
        f"波段起漲最低點: [bold]{w.trough_date}[/bold] ({w.trough_price:,.2f}) → "
        f"現價 [bold]{w.latest_close:,.2f}[/bold] "
        f"([{'red' if w.gain_from_trough_pct >= 0 else 'green'}]{w.gain_from_trough_pct:+.2f}%[/])  |  "
        f"含整理期觀察起點: [bold]{w.observation_start}[/bold]\n"
        f"前{top_n}大真主力合計持有: [bold magenta]{res.top_branches_total_lots:,} 張[/bold magenta]"
    )
    if res.has_resonance_sell:
        header_text += f"  |  [bold red]⚠️ 前{top_n}大 {res.top3_net_sell_count} 家同日淨賣(共振出貨)[/bold red]"

    console.print(Panel(header_text, expand=False))

    t1 = Table(title=f"前{top_n}大主力分點每日監控（已排除隔日沖）", show_lines=True)
    t1.add_column("名次", justify="center", style="bold", no_wrap=True)
    t1.add_column("主力分點", style="cyan", no_wrap=True)
    t1.add_column("多週期交叉", justify="left", no_wrap=True)
    t1.add_column("目前持有(張)", justify="right", style="bold magenta", no_wrap=True)
    t1.add_column("平均成本", justify="right", no_wrap=True)
    t1.add_column("較成本高(%)", justify="right", no_wrap=True)
    t1.add_column("今日動作 (佔持股%)", justify="right", no_wrap=True)
    t1.add_column("大單賣出警示", justify="left")

    for br in res.top_branches:
        tags_disp: list[str] = []
        if getattr(br, "is_key_broker", False):
            tags_disp.append("[bold yellow][🌟關鍵][/bold yellow]")
        if br.is_geo_broker:
            tags_disp.append("[yellow][地緣][/yellow]")
        name_disp = f"{br.name} {' '.join(tags_disp)}" if tags_disp else br.name
        profit_disp = (
            f"[bold red]{br.profit_pct:+.1f}% ⚠️出貨風險[/bold red]"
            if br.is_high_profit_risk
            else f"[{'red' if br.profit_pct >= 0 else 'green'}]{br.profit_pct:+.1f}%[/]"
        )
        sigs: list[str] = []
        if br.sell_alerts:
            sigs.append("[bold red]🚨 " + "\n".join(br.sell_alerts) + "[/bold red]")
        if br.buy_alerts:
            sigs.append("[bold cyan]📈 " + "\n".join(br.buy_alerts) + "[/bold cyan]")
        alert_disp = "\n".join(sigs) if sigs else "[green]正常[/green]"
        t1.add_row(
            str(br.rank),
            name_disp,
            _format_period_badge(br.period_tags, br.period_count),
            f"{br.wave_net_lots:,}",
            f"{br.avg_cost:,.2f}",
            profit_disp,
            _format_today_action(
                br.today_net_lots, br.today_net_pct_of_holding, br.is_new_position
            ),
            alert_disp,
        )
    console.print(t1)

    if res.excluded_day_traders:
        ex_lines = [
            f"• {ex.name} (區間淨買 {ex.wave_net_lots:,} 張) → {'、'.join(ex.eval_info.reasons)}"
            for ex in res.excluded_day_traders[:5]
        ]
        console.print(
            "[dim]已依個股行為排除隔日沖分點：\n" + "\n".join(ex_lines) + "[/dim]"
        )
    console.print()


def _fmt_price(price: float) -> str:
    return f"{price:.2f}".rstrip("0").rstrip(".")


def _fmt_signed(val: int, unit: str = "") -> str:
    if val > 0:
        return f"+{val:,}{unit}"
    if val < 0:
        return f"−{abs(val):,}{unit}"
    return f"0{unit}"


def _fmt_pct(pct: float) -> str:
    if pct > 0:
        return f"+{pct:.1f}%"
    if pct < 0:
        return f"−{abs(pct):.1f}%"
    return "0.0%"


def _fmt_vol_pct(lots: int, vol: int) -> str:
    if vol <= 0 or lots == 0:
        return ""
    pct = abs(lots) / vol * 100.0
    if pct >= 1.0:
        return f"（佔量{pct:.0f}%）"
    if pct >= 0.1:
        return f"（佔量{pct:.1f}%）"
    return ""


def _pick_key_broker(
    r: StockChipMonitorResult,
) -> tuple[Any, bool, bool, int, int] | None:
    """Pick the single most significant broker across foreign and domestic top branches."""
    candidates: dict[str, tuple[float, Any, bool, bool, int, int]] = {}
    for is_for, branches in ((True, r.foreign_branches), (False, r.top_branches)):
        for b in branches:
            if b.consecutive_net_sell_days >= b.consecutive_net_buy_days:
                is_sell = True
                cons_days = b.consecutive_net_sell_days
                cons_lots = b.consecutive_net_sell_lots
            else:
                is_sell = False
                cons_days = b.consecutive_net_buy_days
                cons_lots = b.consecutive_net_buy_lots

            score = 0.0
            if cons_days >= 2:
                score = cons_days * 100.0 + abs(cons_lots) * 0.1 + abs(b.today_net_pct_of_volume) * 10.0
            elif abs(b.today_net_pct_of_volume) >= 2.0:
                score = abs(b.today_net_pct_of_volume) * 20.0

            if score <= 0:
                continue
            prev = candidates.get(b.name)
            if prev is None or score > prev[0]:
                candidates[b.name] = (score, b, is_for, is_sell, cons_days, cons_lots)

    if not candidates:
        return None
    best = max(candidates.values(), key=lambda x: x[0])
    return best[1], best[2], best[3], best[4], best[5]


def _build_market_overview(results: list[StockChipMonitorResult]) -> str:
    n = len(results)
    chg_list = [r.wave_info.today_change_pct for r in results]
    min_chg, max_chg = min(chg_list), max(chg_list)

    if all(c <= -9.5 for c in chg_list):
        return f"{n}檔全數跌停，個股訊號參考性低"
    if all(c <= -7.0 for c in chg_list):
        return f"{n}檔全數跌停或近跌停（{_fmt_pct(max_chg)}～{_fmt_pct(min_chg)}），個股訊號參考性低"

    heavy_down = sum(1 for c in chg_list if c <= -5.0)
    if n >= 3 and heavy_down >= (n * 2 + 2) // 3:
        return f"{n}檔多數重挫（{heavy_down}檔跌幅逾5%），留意系統性賣壓"

    if all(c >= 9.5 for c in chg_list):
        return f"{n}檔全數漲停，留意隔日獲利了結"
    if all(c >= 7.0 for c in chg_list):
        return f"{n}檔全數大漲或漲停（{_fmt_pct(min_chg)}～{_fmt_pct(max_chg)}），個股訊號偏多"

    counts = {"sell": 0, "divergent": 0, "buy": 0, "stable": 0}
    for r in results:
        counts[r.overall_status] = counts.get(r.overall_status, 0) + 1
    parts = []
    if counts["sell"]:
        parts.append(f"偏空 {counts['sell']}")
    if counts["divergent"]:
        parts.append(f"分歧 {counts['divergent']}")
    if counts["buy"]:
        parts.append(f"偏多 {counts['buy']}")
    if counts["stable"]:
        parts.append(f"平穩 {counts['stable']}")
    return f"監控 {n} 檔｜" + "・".join(parts)


def _classify_stock(r: StockChipMonitorResult) -> tuple[int, float, str, int, int]:
    """Return (tier, severity_score, icon, foreign_net, domestic_net)."""
    vol = r.wave_info.today_volume_lots
    cf = r.chip_flow
    foreign_net = cf.foreign_chg if cf is not None else sum(b.today_net_lots for b in r.foreign_branches)
    domestic_net = sum(b.today_net_lots for b in r.top_branches)

    foreign_vol_pct = (abs(foreign_net) / vol * 100.0) if vol > 0 else 0.0
    domestic_vol_pct = (abs(domestic_net) / vol * 100.0) if vol > 0 else 0.0

    is_high_sell = (
        r.is_chips_to_retail
        or r.overall_status == "sell"
        or r.has_resonance_sell
        or len(r.alerts) > 0
        or (foreign_net < 0 and foreign_vol_pct >= 10.0)
    )
    if is_high_sell:
        score = (
            foreign_vol_pct
            + (20.0 if r.is_chips_to_retail else 0.0)
            + (10.0 if r.has_resonance_sell else 0.0)
            + domestic_vol_pct * 0.5
        )
        return 3, score, "🔴", foreign_net, domestic_net

    is_notable = r.overall_status == "divergent" or (
        r.overall_status == "buy"
        and (
            len(r.buy_alerts) > 0
            or r.has_resonance_buy
            or (foreign_net > 0 and foreign_vol_pct >= 5.0)
            or (domestic_net > 0 and domestic_vol_pct >= 3.0)
        )
    )
    if is_notable:
        score = max(foreign_vol_pct, domestic_vol_pct) + (5.0 if r.overall_status == "divergent" else 0.0)
        icon = "🟢" if (r.overall_status == "buy" and r.wave_info.today_change_pct > -7.0) else "⚪"
        return 2, score, icon, foreign_net, domestic_net

    return 1, max(foreign_vol_pct, domestic_vol_pct), "⚪", foreign_net, domestic_net


def _fmt_broker_name(kb: Any) -> str:
    star = "🌟" if getattr(kb, "is_key_broker", False) else ""
    return f"{star}{html.escape(kb.name, quote=False)}"


def _build_conclusion(
    r: StockChipMonitorResult,
    foreign_net: int,
    domestic_net: int,
    key_broker: tuple[Any, bool, bool, int, int] | None,
) -> str:
    vol = r.wave_info.today_volume_lots
    chg = r.wave_info.today_change_pct
    cf = r.chip_flow
    foreign_vol_pct = (abs(foreign_net) / vol * 100.0) if vol > 0 else 0.0

    if r.is_chips_to_retail and cf is not None and cf.margin_available:
        return f"外資 {_fmt_signed(foreign_net)}、融資 {_fmt_signed(cf.margin_chg)}，籌碼轉向散戶"

    if foreign_net < 0 and foreign_vol_pct >= 10.0 and r.buy_alerts:
        return "外資賣壓為主，標題「建倉」不可信"

    if r.overall_status == "sell" or (foreign_net < 0 and foreign_vol_pct >= 5.0):
        if key_broker is not None:
            kb, kb_is_for, kb_is_sell, kb_days, kb_lots = key_broker
            if kb_is_sell and kb_days >= 2:
                prefix = "外資連賣，" if kb_is_for else f"第{kb.rank}主力 "
                return f"{prefix}{_fmt_broker_name(kb)}連{kb_days}天累計 {_fmt_signed(-kb_lots)}"
        if foreign_net < 0 and foreign_vol_pct >= 10.0 and domestic_net > 0:
            return f"外資賣壓為主（佔量{foreign_vol_pct:.0f}%），本土零星承接"
        if r.has_resonance_sell:
            return "本土前三大主力同步減碼"
        return f"外資淨賣 {_fmt_signed(foreign_net)}張（佔量{foreign_vol_pct:.0f}%）"

    if r.overall_status == "divergent":
        if chg <= -7.0 and foreign_net > 0 and domestic_net > 0:
            state = "跌停" if chg <= -9.5 else "重挫"
            return f"有承接但仍{state}，量 {vol:,}張"
        if key_broker is not None:
            kb, _, kb_is_sell, kb_days, kb_lots = key_broker
            if kb_days >= 2:
                act = "連賣" if kb_is_sell else "連買"
                signed_lots = _fmt_signed(-kb_lots if kb_is_sell else kb_lots)
                return f"多空分歧，{_fmt_broker_name(kb)}{act}{kb_days}天 {signed_lots}"
        return f"外資與本土多空分歧，量 {vol:,}張"

    if key_broker is not None:
        kb, _, kb_is_sell, kb_days, kb_lots = key_broker
        if not kb_is_sell and kb_days >= 2:
            return f"{_fmt_broker_name(kb)}連{kb_days}天累計 {_fmt_signed(kb_lots)}"
    return f"買盤主導，量 {vol:,}張"


def _build_single_line_note(
    r: StockChipMonitorResult,
    domestic_net: int,
    key_broker: tuple[Any, bool, bool, int, int] | None,
) -> str:
    vol = r.wave_info.today_volume_lots
    if key_broker is not None:
        kb, kb_is_for, kb_is_sell, kb_days, _ = key_broker
        if kb_is_sell and kb_days >= 3:
            role = "外資" if kb_is_for else f"第{kb.rank}主力"
            return f"⚠️ {_fmt_broker_name(kb)}{role}連{kb_days}賣"

    all_branches = r.top_branches + r.foreign_branches
    buy_sum = sum(b.today_net_lots for b in all_branches if b.today_net_lots > 0)
    sell_sum = sum(b.today_net_lots for b in all_branches if b.today_net_lots < 0)
    if abs(domestic_net) <= max(15, int(vol * 0.005)) and buy_sum < 100 and abs(sell_sum) < 100:
        return f"本土前{len(r.top_branches) or 5}持股穩定"
    if buy_sum > 0 or sell_sum < 0:
        return f"買方{_fmt_signed(buy_sum)}/賣方{_fmt_signed(sell_sum)}"
    return f"本土前{len(r.top_branches) or 5} {_fmt_signed(domestic_net)}"


def build_telegram_daily_text(
    results: list[StockChipMonitorResult],
    html_path: Path | None = None,
    report_url: str | None = None,
) -> str:
    """Build a concise, conclusion-first daily Telegram digest."""
    if not results:
        return ""

    raw_date = results[0].data_date
    short_date = raw_date[5:].replace("-", "/") if len(raw_date) == 10 and raw_date[4] == "-" else raw_date

    lines = [
        f"📊 <b>籌碼日報 {short_date}</b>",
        _build_market_overview(results),
    ]

    classified = [(_classify_stock(r), r) for r in results]
    classified.sort(key=lambda item: (item[0][0], item[0][1]), reverse=True)

    compact_started = False
    for (tier, _, icon, foreign_net, domestic_net), r in classified:
        w = r.wave_info
        vol = w.today_volume_lots
        cf = r.chip_flow
        key_broker = _pick_key_broker(r)

        if tier >= 2:
            margin_tag = ""
            if cf is not None and cf.margin_available and cf.margin_chg != 0:
                margin_tag = f"｜融資 {_fmt_signed(cf.margin_chg)}"
            elif cf is not None and not cf.margin_available and r.is_chips_to_retail:
                margin_tag = "｜融資待更新"

            dom_label = f"本土前{len(r.top_branches) or 5}"
            f_str = f"{_fmt_signed(foreign_net, '張')}{_fmt_vol_pct(foreign_net, vol)}"
            d_str = f"{_fmt_signed(domestic_net)}"
            conclusion = _build_conclusion(r, foreign_net, domestic_net, key_broker)

            safe_name = html.escape(r.stock_name, quote=False)
            lines.append(
                f"\n{icon} <b>{safe_name}</b> {_fmt_price(w.latest_close)} ({_fmt_pct(w.today_change_pct)})"
            )
            lines.append(f"  外資 {f_str}｜{dom_label} {d_str}{margin_tag}")
            lines.append(f"  → {conclusion}")
        else:
            if not compact_started:
                lines.append("")
                compact_started = True
            safe_name = html.escape(r.stock_name, quote=False)
            note = _build_single_line_note(r, domestic_net, key_broker)
            lines.append(
                f"{icon} <b>{safe_name}</b> {_fmt_price(w.latest_close)} ({_fmt_pct(w.today_change_pct)})｜外資 {_fmt_signed(foreign_net)}｜{note}"
            )

    all_margin_missing = all(not (r.chip_flow and r.chip_flow.margin_available) for r in results)
    if all_margin_missing:
        lines.append("\n融資變動：資料未更新")

    if report_url:
        safe_url = html.escape(report_url, quote=True)
        lines.append(f'📄 <a href="{safe_url}">點此開啟今日完整分點籌碼報表</a>')
    elif html_path:
        lines.append("📄 詳細分點報表請點擊下方 HTML 附件開啟。")
    return "\n".join(lines)


def build_telegram_alert_text(
    results: list[StockChipMonitorResult],
    html_path: Path | None = None,
    report_url: str | None = None,
) -> str:
    triggered = [r for r in results if r.should_notify_telegram]
    if not triggered:
        return ""
    return build_telegram_daily_text(results, html_path=html_path, report_url=report_url)


def build_telegram_normal_text(
    results: list[StockChipMonitorResult],
    top_n: int = 5,
    html_path: Path | None = None,
    report_url: str | None = None,
) -> str:
    return build_telegram_daily_text(results, html_path=html_path, report_url=report_url)


def export_results(results: list[StockChipMonitorResult], out_dir: Path) -> None:
    if not results:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    report_date = results[0].data_date

    serializable = []
    for r in results:
        d = asdict(r)
        d["wave_info"].pop("daily_avg_prices", None)
        d["wave_info"].pop("trading_dates", None)
        serializable.append(d)

    json_path = out_dir / f"chip_report_{report_date}.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)

    csv_path = out_dir / f"chip_report_{report_date}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "資料日期",
                "股票代號",
                "股票名稱",
                "波段起漲最低點",
                "觀察起點(含整理期)",
                "最新收盤",
                "波段漲幅(%)",
                "名次",
                "主力分點",
                "地緣主力",
                "多週期確認",
                "目前持有(張)",
                "平均成本",
                "較成本高(%)",
                "今日買進(張)",
                "今日賣出(張)",
                "今日淨買賣(張)",
                "今日動作佔持股(%)",
                "連續淨賣天數",
                "共振出貨",
                "大單賣出警示",
            ]
        )
        for r in results:
            for br in r.top_branches:
                writer.writerow(
                    [
                        r.data_date,
                        r.stock_id,
                        r.stock_name,
                        r.wave_info.trough_date,
                        r.wave_info.observation_start,
                        r.wave_info.latest_close,
                        r.wave_info.gain_from_trough_pct,
                        br.rank,
                        br.name,
                        "Y" if br.is_geo_broker else "N",
                        " ".join(br.period_tags),
                        br.wave_net_lots,
                        br.avg_cost,
                        br.profit_pct,
                        br.today_buy_lots,
                        br.today_sell_lots,
                        br.today_net_lots,
                        br.today_net_pct_of_holding,
                        br.consecutive_net_sell_days,
                        "Y" if r.has_resonance_sell else "N",
                        "；".join(br.sell_alerts),
                    ]
                )
    console.print(f"[bold green]已匯出報表:[/bold green] {csv_path} 與 {json_path}")


def run_once(args: argparse.Namespace) -> None:
    cfg = load_config(Path(args.config))
    defaults = cfg.get("defaults", {})
    geo_map: dict[str, list[str]] = cfg.get("geo_brokers", {})
    tg_cfg: dict[str, Any] = cfg.get("telegram", {})

    wave_search_days = defaults.get("wave_search_days", 120)
    pre_wave_days = args.pre_days if args.pre_days is not None else defaults.get("pre_wave_days", 30)
    top_n = defaults.get("top_n_branches", 3)
    geo_boost_default = defaults.get("geo_boost_default", 1.35)
    geo_boost_small = defaults.get("geo_boost_small_cap", 1.5)
    profit_risk_pct = defaults.get("profit_risk_pct", 30.0)

    if args.stocks:
        targets = [{"stock_id": sid.strip()} for sid in args.stocks]
    else:
        csv_source = args.csv or cfg.get("watchlist_csv")
        if csv_source and Path(csv_source).exists():
            targets = load_watchlist_csv(Path(csv_source))
            console.print(f"[dim]已從 CSV 載入 {len(targets)} 檔監控標的: {csv_source}[/dim]")
        else:
            targets = cfg.get("watchlist", [])

    if not targets:
        console.print("[red]沒有設定任何監控股票，請在 config.json 加入 watchlist 或於命令列傳入股票代號。[/red]")
        return

    results: list[StockChipMonitorResult] = []
    for item in targets:
        sid = str(item["stock_id"])
        name_override = item.get("name")
        explicit_start = args.start or item.get("wave_start")
        is_small = bool(item.get("small_cap", False))
        geo_kws = geo_map.get(sid, [])
        geo_boost = geo_boost_small if is_small else geo_boost_default

        try:
            with console.status(f"[cyan]正在分析 {sid} {name_override or ''} 多週期主力籌碼與隔日沖過濾...[/cyan]"):
                res = analyze_stock(
                    stock_id=sid,
                    name_override=name_override,
                    explicit_wave_start=explicit_start,
                    wave_search_days=wave_search_days,
                    pre_wave_days=pre_wave_days,
                    top_n=top_n,
                    geo_keywords=geo_kws,
                    geo_boost=geo_boost,
                    profit_risk_pct=profit_risk_pct,
                )
            render_result(res, top_n=top_n)
            results.append(res)
        except Exception as exc:
            console.print(f"[bold red]分析 {sid} 時發生錯誤:[/bold red] {exc}")

    if not results:
        return

    dated_html: Path | None = None
    latest_html: Path | None = None
    if args.export:
        export_results(results, Path("reports"))

    if args.html or args.open_html or args.export or args.telegram or tg_cfg.get("enabled", False):
        dated_html, latest_html = generate_html_report(results, Path("reports"), top_n=top_n)
        console.print(f"[bold green]已產生 HTML 報表:[/bold green] {dated_html} (最新捷徑: {latest_html})")

    alert_lines = [
        f"• [bold]{r.stock_id} {r.stock_name}[/bold]: {al}"
        for r in results
        for al in (r.alerts + r.foreign_alerts)
    ]
    buy_alert_lines = [
        f"• [bold]{r.stock_id} {r.stock_name}[/bold]: {bal}"
        for r in results
        for bal in (r.buy_alerts + r.foreign_buy_alerts)
    ]
    if alert_lines:
        console.print(
            Panel(
                "\n".join(alert_lines),
                title="[bold red]🚨 今日大單賣出警示總覽[/bold red]",
                border_style="red",
            )
        )
    if buy_alert_lines:
        console.print(
            Panel(
                "\n".join(buy_alert_lines),
                title="[bold cyan]📈 今日主力持續建倉 / 加碼通知[/bold cyan]",
                border_style="cyan",
            )
        )
    report_url = args.report_url or tg_cfg.get("report_url")
    if alert_lines or buy_alert_lines:
        if args.telegram or tg_cfg.get("enabled", False):
            msg = build_telegram_alert_text(results, html_path=dated_html, report_url=report_url)
            ok, info = send_telegram_message(
                msg,
                bot_token=tg_cfg.get("bot_token"),
                chat_id=tg_cfg.get("chat_id"),
            )
            style = "green" if ok else "yellow"
            console.print(f"[{style}]Telegram 通知: {info}[/{style}]")
            if ok and dated_html:
                ok_doc, info_doc = send_telegram_document(
                    dated_html,
                    caption=f"台股主力籌碼詳細報表 ({results[0].data_date})",
                    bot_token=tg_cfg.get("bot_token"),
                    chat_id=tg_cfg.get("chat_id"),
                )
                console.print(f"[{'green' if ok_doc else 'yellow'}]Telegram 附件: {info_doc}[/]")
    else:
        console.print(
            Panel(
                f"所有監控標的今日前{top_n}大真主力皆無觸發大單賣出條件。",
                title="[bold green]今日籌碼監控總覽[/bold green]",
                border_style="green",
            )
        )
        if (args.telegram or tg_cfg.get("enabled", False)) and (
            args.tg_always or tg_cfg.get("notify_when_normal", False)
        ):
            msg = build_telegram_normal_text(results, top_n=top_n, html_path=dated_html, report_url=report_url)
            ok, info = send_telegram_message(
                msg,
                bot_token=tg_cfg.get("bot_token"),
                chat_id=tg_cfg.get("chat_id"),
            )
            style = "green" if ok else "yellow"
            console.print(f"[{style}]Telegram 通知: {info}[/{style}]")
            if ok and dated_html:
                ok_doc, info_doc = send_telegram_document(
                    dated_html,
                    caption=f"台股主力籌碼詳細報表 ({results[0].data_date})",
                    bot_token=tg_cfg.get("bot_token"),
                    chat_id=tg_cfg.get("chat_id"),
                )
                console.print(f"[{'green' if ok_doc else 'yellow'}]Telegram 附件: {info_doc}[/]")

    if args.open_html and latest_html:
        webbrowser.open(latest_html.resolve().as_uri())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="台股每日主力籌碼監控：波段起漲區間多週期確認、行為排除隔日沖、地緣加分與 Telegram 大單賣出警示"
    )
    parser.add_argument(
        "stocks",
        nargs="*",
        help="指定要監控的台股代號（例如: 2330 2317 6488）；若不指定則讀取 config.json",
    )
    parser.add_argument("--config", default="config.json", help="設定檔路徑 (預設: config.json)")
    parser.add_argument("--csv", default=None, help="指定監控股票清單 CSV 路徑 (預設讀取 config.json 的 watchlist_csv)")
    parser.add_argument("--start", default=None, help="手動指定波段起漲日期 YYYY-MM-DD")
    parser.add_argument("--pre-days", type=int, default=None, help="起漲最低點往前推的整理期交易日數 (預設: 30)")
    parser.add_argument("--telegram", action="store_true", help="啟用 Telegram 推播")
    parser.add_argument("--tg-always", action="store_true", help="即使今日無異常也發送 Telegram 正常彙報")
    parser.add_argument("--export", action="store_true", help="匯出今日監控結果至 reports/ (CSV、JSON 與 HTML)")
    parser.add_argument("--report-url", default=None, help="線上報表連結（若設定則附帶於 Telegram 訊息中）")
    parser.add_argument("--html", action="store_true", help="產生可視化 HTML 報表至 reports/")
    parser.add_argument("--open-html", action="store_true", help="產生 HTML 報表並自動以瀏覽器開啟")
    parser.add_argument("--schedule", default=None, help="每日定時執行時間 HH:MM (例如: 15:30)")

    args = parser.parse_args()

    if args.schedule:
        console.print(f"[cyan]已啟動每日定時監控排程：每天 {args.schedule} 自動執行 (按 Ctrl+C 結束)...[/cyan]")
        run_once(args)
        schedule.every().day.at(args.schedule).do(run_once, args=args)
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
